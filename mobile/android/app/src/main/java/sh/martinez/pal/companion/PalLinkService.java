package sh.martinez.pal.companion;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Foreground relay that lets a bonded PAL Round reach the PAL gateway through
 * this phone (firmware/pal-round/BLE_LINK.md). The phone is a dumb TCP pipe:
 * the Round does its own TLS, we only carry ciphertext to {gateway host}:443.
 *
 * Threading: every bit of link state (GATT, the op queue, the stream map) is
 * owned by one HandlerThread. GATT callbacks and socket worker threads post to
 * it. Each stream has a socket reader (blocks on credit, then on read) and a
 * writer (blocks on its queue). No timers run while idle: reconnection is the
 * OS's autoConnect, and the only delayed callbacks are one-shot GATT-op
 * timeouts / write retries that exist only while an op is in flight.
 */
@SuppressLint("MissingPermission")   // BLUETOOTH_CONNECT is checked before any GATT use
public class PalLinkService extends Service {

    static final String TAG = "PalLink";
    private static final String CHANNEL_ID = "pal_link";
    private static final int NOTIF_ID = 0x5041;   // "PA"
    private static final int REQUEST_MTU = 517;
    private static final int CONNECT_TIMEOUT_MS = 10_000;
    private static final long OP_TIMEOUT_MS = 10_000;
    private static final long WRITE_RETRY_MS = 15;
    private static final int MAX_WRITE_RETRIES = 200;   // ~3 s of "busy"

    static void start(Context ctx) {
        ContextCompat.startForegroundService(ctx, new Intent(ctx, PalLinkService.class));
    }

    static void stop(Context ctx) {
        ctx.stopService(new Intent(ctx, PalLinkService.class));
    }

    // ---- owned by the link thread ------------------------------------------
    private HandlerThread thread;
    private Handler h;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic txChar;
    private BluetoothGattCharacteristic rxChar;
    private String address;
    private volatile String allowedHost;
    private volatile int mtu = PalLinkProtocol.DEFAULT_MTU;
    private boolean gattConnected = false;
    private boolean linkUp = false;          // device HELLO received
    private int maxStreams = 255;            // until the device's HELLO says otherwise
    private final Map<Integer, Stream> streams = new HashMap<>();
    private final ArrayDeque<Op> queue = new ArrayDeque<>();
    private Op current;
    private int writeRetries = 0;
    private boolean destroyed = false;
    private boolean receiverRegistered = false;
    private String lastNotifText;

    private final Runnable opTimeout = () -> {
        Log.w(TAG, "GATT op timed out: " + (current != null ? current.name : "?") + " — resetting link");
        resetGatt();
    };
    private final Runnable pumpRunnable = this::pump;

    // Reconnecting after a drop: a few quick direct connects first (seconds,
    // while the Round is most likely right there), then the OS's low-power
    // autoConnect. Some stacks let a pending autoConnect go stale, so while
    // still disconnected the client is re-armed every few minutes.
    private static final int DIRECT_TRIES = 3;
    private static final long DIRECT_REARM_MS = 35_000;   // Android gives up a direct connect after ~30 s
    private static final long AUTO_REARM_MS = 180_000;
    private int directLeft = 0;
    private final Runnable rearm = () -> {
        if (destroyed || gattConnected) return;
        Log.i(TAG, "still disconnected — re-arming the connection");
        closeGatt();
        connect();
    };

    // ---- lifecycle -----------------------------------------------------------

    @Override
    public void onCreate() {
        super.onCreate();
        thread = new HandlerThread("pal-link");
        thread.start();
        h = new Handler(thread.getLooper());
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Must call startForeground promptly after startForegroundService.
        if (!enterForeground()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!PalLinkState.shouldRun(this)) {
            Log.i(TAG, "link not enabled/paired/permitted — stopping");
            stopSelf();
            return START_NOT_STICKY;
        }
        final String addr = PalLinkState.address(this);
        allowedHost = PalLinkState.gatewayHost(this);
        h.post(() -> {
            if (!receiverRegistered) {
                ContextCompat.registerReceiver(this, adapterReceiver,
                    new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED), null, h,
                    ContextCompat.RECEIVER_NOT_EXPORTED);
                receiverRegistered = true;
            }
            if (gatt != null && addr.equals(address)) return;   // already running for this device
            closeGatt();
            address = addr;
            connect();
        });
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        h.post(() -> {
            destroyed = true;
            if (receiverRegistered) {
                try { unregisterReceiver(adapterReceiver); } catch (Exception ignored) { }
                receiverRegistered = false;
            }
            closeGatt();
            PalLinkState.setLive(false, false, 0);
            thread.quitSafely();
        });
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    private boolean enterForeground() {
        int type = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE;
        }
        try {
            ServiceCompat.startForeground(this, NOTIF_ID, buildNotification(statusText()), type);
            return true;
        } catch (Exception e) {
            // SecurityException on 14+ without BLUETOOTH_CONNECT, or a background-start ban.
            Log.w(TAG, "startForeground failed", e);
            return false;
        }
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "PAL Round link",
            NotificationManager.IMPORTANCE_MIN);
        ch.setDescription("Shown while this phone shares its internet with a PAL Round over Bluetooth.");
        ch.setShowBadge(false);
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.createNotificationChannel(ch);
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("Sharing internet with PAL Round")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setContentIntent(pi)
            .build();
    }

    private String statusText() {
        if (!linkUp) return "Waiting for PAL Round";
        int n = streams.size();
        return n == 0 ? "Connected" : "Connected · " + n + (n == 1 ? " stream" : " streams");
    }

    /** Publish live state to the plugin + refresh the notification (only on change). */
    private void publish() {
        PalLinkState.setLive(gattConnected, linkUp, streams.size());
        if (destroyed) return;
        String text = statusText();
        if (text.equals(lastNotifText)) return;
        lastNotifText = text;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            try { nm.notify(NOTIF_ID, buildNotification(text)); } catch (Exception ignored) { }
        }
    }

    // ---- connection management ---------------------------------------------

    private void connect() {
        if (destroyed || address == null) return;
        if (!PalLinkState.hasConnectPermission(this)) {
            Log.w(TAG, "BLUETOOTH_CONNECT revoked — stopping");
            stopSelf();
            return;
        }
        BluetoothManager bm = getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter = bm != null ? bm.getAdapter() : null;
        if (adapter == null || !adapter.isEnabled()) {
            Log.i(TAG, "Bluetooth off — waiting for it to come back");
            publish();
            return;
        }
        BluetoothDevice dev;
        try {
            dev = adapter.getRemoteDevice(address);
        } catch (IllegalArgumentException e) {
            Log.w(TAG, "bad stored address " + address);
            stopSelf();
            return;
        }
        boolean direct = directLeft > 0;
        if (direct) directLeft--;
        Log.i(TAG, "connectGatt(" + (direct ? "direct" : "autoConnect") + ") " + address);
        // autoConnect=true: the controller's low-duty background scan brings the
        // link up whenever the Round advertises — no app-side scanning/polling.
        gatt = dev.connectGatt(this, !direct, gattCallback, BluetoothDevice.TRANSPORT_LE);
        if (gatt == null) Log.w(TAG, "connectGatt returned null");
        h.removeCallbacks(rearm);
        h.postDelayed(rearm, direct ? DIRECT_REARM_MS : AUTO_REARM_MS);
        publish();
    }

    /** Close the GATT client + every stream. No frames (the link is gone). */
    private void closeGatt() {
        abortAllStreams();
        queue.clear();
        current = null;
        h.removeCallbacks(opTimeout);
        h.removeCallbacks(pumpRunnable);
        if (gatt != null) {
            try { gatt.close(); } catch (Exception ignored) { }
            gatt = null;
        }
        txChar = rxChar = null;
        gattConnected = false;
        linkUp = false;
        mtu = PalLinkProtocol.DEFAULT_MTU;
        publish();
    }

    /** Hard reset after a stuck/failed op: drop everything and re-arm autoConnect. */
    private void resetGatt() {
        closeGatt();
        connect();
    }

    private final BroadcastReceiver adapterReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context ctx, Intent intent) {
            int state = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR);
            if (state == BluetoothAdapter.STATE_TURNING_OFF || state == BluetoothAdapter.STATE_OFF) {
                if (gatt != null) { Log.i(TAG, "Bluetooth turning off"); closeGatt(); }
            } else if (state == BluetoothAdapter.STATE_ON) {
                if (gatt == null) { Log.i(TAG, "Bluetooth on — re-arming"); connect(); }
            }
        }
    };

    private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            h.post(() -> onConnState(g, status, newState));
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int newMtu, int status) {
            h.post(() -> {
                if (g != gatt) return;
                if (status == BluetoothGatt.GATT_SUCCESS) mtu = newMtu;
                Log.i(TAG, "MTU " + mtu + " (status " + status + ")");
                opDone("mtu");
            });
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            h.post(() -> {
                if (g != gatt) return;
                onServices(status);
            });
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
            h.post(() -> {
                if (g != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    Log.w(TAG, "CCCD write failed " + status);
                    resetGatt();
                    return;
                }
                opDone("cccd");
            });
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            h.post(() -> {
                if (g != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    Log.w(TAG, "frame write failed " + status + " — resetting link");
                    resetGatt();
                    return;
                }
                opDone("write");
            });
        }

        // API 33+: value delivered alongside (the old overload isn't called then).
        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
            if (value == null) return;
            final byte[] v = value.clone();
            h.post(() -> { if (g == gatt) onNotify(v); });
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            if (Build.VERSION.SDK_INT >= 33) return;
            byte[] value = c.getValue();
            if (value == null) return;
            final byte[] v = value.clone();
            h.post(() -> { if (g == gatt) onNotify(v); });
        }
    };

    private void onConnState(BluetoothGatt g, int status, int newState) {
        if (g != gatt) return;
        if (newState == BluetoothProfile.STATE_CONNECTED) {
            Log.i(TAG, "GATT connected (status " + status + ")");
            h.removeCallbacks(rearm);
            directLeft = 0;
            gattConnected = true;
            linkUp = false;
            mtu = PalLinkProtocol.DEFAULT_MTU;
            queue.clear();
            current = null;
            enqueue(new Op("mtu", true) {
                int run() { return gatt.requestMtu(REQUEST_MTU) ? WAIT : FAIL; }
            });
            enqueue(new Op("phy", false) {
                int run() {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        gatt.setPreferredPhy(BluetoothDevice.PHY_LE_2M_MASK,
                            BluetoothDevice.PHY_LE_2M_MASK, BluetoothDevice.PHY_OPTION_NO_PREFERRED);
                    }
                    return DONE;
                }
            });
            enqueue(new Op("discover", true) {
                int run() { return gatt.discoverServices() ? WAIT : FAIL; }
            });
            publish();
        } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
            Log.i(TAG, "GATT disconnected (status " + status + ") — reconnecting");
            if (gattConnected) directLeft = DIRECT_TRIES;   // a drop: the Round is probably still near
            // Fresh client every time: some stacks don't re-arm autoConnect after an
            // error status. A pending autoConnect costs nothing (controller filter).
            closeGatt();
            connect();
        }
    }

    @SuppressWarnings("deprecation")
    private void onServices(int status) {
        if (status != BluetoothGatt.GATT_SUCCESS) {
            Log.w(TAG, "service discovery failed " + status);
            resetGatt();
            return;
        }
        BluetoothGattService svc = gatt.getService(PalLinkProtocol.SERVICE_UUID);
        txChar = svc != null ? svc.getCharacteristic(PalLinkProtocol.TX_UUID) : null;
        rxChar = svc != null ? svc.getCharacteristic(PalLinkProtocol.RX_UUID) : null;
        final BluetoothGattDescriptor cccd = txChar != null
            ? txChar.getDescriptor(PalLinkProtocol.CCCD_UUID) : null;
        if (rxChar == null || cccd == null) {
            Log.w(TAG, "PAL link service/characteristics missing on device");
            opDone("discover");
            // Not our device (or old firmware): nothing to relay; stay connected idle.
            return;
        }
        enqueue(new Op("cccd", true) {
            int run() {
                if (!gatt.setCharacteristicNotification(txChar, true)) return FAIL;
                byte[] en = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE;
                if (Build.VERSION.SDK_INT >= 33) {
                    int r = gatt.writeDescriptor(cccd, en);
                    if (r == BluetoothStatusCodes.SUCCESS) return WAIT;
                    return r == BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY ? RETRY : FAIL;
                }
                cccd.setValue(en);
                return gatt.writeDescriptor(cccd) ? WAIT : RETRY;
            }
        });
        // HELLO right after the subscription (queued behind the CCCD write).
        sendFrame(PalLinkProtocol.hello(System.currentTimeMillis()), "hello");
        opDone("discover");
    }

    // ---- serialized GATT op queue -------------------------------------------

    private abstract static class Op {
        static final int WAIT = 0, DONE = 1, RETRY = 2, FAIL = 3;
        final String name;
        final boolean waits;
        Op(String name, boolean waits) { this.name = name; this.waits = waits; }
        /** Start the op: WAIT for its callback, DONE if synchronous, RETRY if busy, FAIL. */
        abstract int run();
    }

    private void enqueue(Op op) {
        queue.add(op);
        pump();
    }

    /** The in-flight op's callback arrived. Ignores callbacks that don't match it
     *  (stray ones after a reset, or a peer-initiated MTU exchange). */
    private void opDone(String expect) {
        if (current == null) return;
        boolean match = current.name.equals(expect)
            || (expect.equals("write") && current.name.startsWith("write:"));
        if (!match) return;
        h.removeCallbacks(opTimeout);
        current = null;
        writeRetries = 0;
        pump();
    }

    private void pump() {
        while (current == null && gatt != null && gattConnected) {
            Op op = queue.peek();
            if (op == null) return;
            int r;
            try {
                r = op.run();
            } catch (Exception e) {
                Log.w(TAG, "GATT op " + op.name + " threw", e);
                r = Op.FAIL;
            }
            if (r == Op.RETRY) {
                if (++writeRetries > MAX_WRITE_RETRIES) {
                    Log.w(TAG, "GATT op " + op.name + " stayed busy — resetting link");
                    resetGatt();
                    return;
                }
                h.removeCallbacks(pumpRunnable);
                h.postDelayed(pumpRunnable, WRITE_RETRY_MS);
                return;
            }
            queue.poll();
            writeRetries = 0;
            if (r == Op.FAIL) {
                if (op.waits) {
                    // A failed frame write would silently corrupt a stream; a failed
                    // setup op leaves the link useless. Either way: start over.
                    Log.w(TAG, "GATT op " + op.name + " failed — resetting link");
                    resetGatt();
                    return;
                }
                Log.w(TAG, "GATT op " + op.name + " failed (ignored)");
                continue;
            }
            if (r == Op.WAIT) {
                current = op;
                h.postDelayed(opTimeout, OP_TIMEOUT_MS);
                return;
            }
            // DONE: next
        }
    }

    /** Queue one frame (= one write-without-response). Caller guarantees ≤ MTU − 3. */
    @SuppressWarnings("deprecation")
    private void sendFrame(final byte[] frame, String what) {
        if (frame.length > PalLinkProtocol.maxFrame(mtu)) {
            Log.e(TAG, "BUG: frame " + what + " " + frame.length + "B > MTU−3; dropped");
            return;
        }
        enqueue(new Op("write:" + what, true) {
            int run() {
                if (rxChar == null) return FAIL;
                if (Build.VERSION.SDK_INT >= 33) {
                    int r = gatt.writeCharacteristic(rxChar, frame,
                        BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
                    if (r == BluetoothStatusCodes.SUCCESS) return WAIT;
                    return r == BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY ? RETRY : FAIL;
                }
                rxChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
                rxChar.setValue(frame);
                return gatt.writeCharacteristic(rxChar) ? WAIT : RETRY;
            }
        });
    }

    private void requestPriority(final boolean busy) {
        enqueue(new Op(busy ? "prio-high" : "prio-low", false) {
            int run() {
                boolean ok = gatt.requestConnectionPriority(busy
                    ? BluetoothGatt.CONNECTION_PRIORITY_HIGH
                    : BluetoothGatt.CONNECTION_PRIORITY_LOW_POWER);
                return ok ? DONE : FAIL;
            }
        });
    }

    // ---- frames from the device ----------------------------------------------

    private void onNotify(byte[] raw) {
        try {
            handleFrame(PalLinkProtocol.decode(raw));
        } catch (PalLinkProtocol.MalformedFrameException e) {
            Log.w(TAG, "malformed frame: " + e.getMessage());
        } catch (RuntimeException e) {
            // A bad frame must never take the app down.
            Log.e(TAG, "error handling frame", e);
        }
    }

    private void handleFrame(PalLinkProtocol.Frame f) {
        switch (f.type) {
            case PalLinkProtocol.T_HELLO: {
                if (f.stream != 0) { Log.w(TAG, "HELLO on stream " + f.stream); return; }
                try {
                    PalLinkProtocol.DeviceHello hello = PalLinkProtocol.parseDeviceHello(f.payload);
                    if (hello.version != PalLinkProtocol.VERSION) {
                        Log.w(TAG, "device link version " + hello.version + " (we speak "
                            + PalLinkProtocol.VERSION + ")");
                    }
                    maxStreams = hello.maxStreams;
                    Log.i(TAG, "link up: device v" + hello.version + ", max streams " + maxStreams);
                } catch (PalLinkProtocol.MalformedFrameException e) {
                    Log.w(TAG, "bad device HELLO: " + e.getMessage());
                }
                linkUp = true;
                publish();
                return;
            }
            case PalLinkProtocol.T_OPEN:
                handleOpen(f.stream, f.payload);
                return;
            case PalLinkProtocol.T_DATA: {
                Stream s = streams.get(f.stream);
                if (s == null) return;   // raced our CLOSE — drop
                if (!s.opened) {
                    Log.w(TAG, "DATA before OPENED on stream " + f.stream);
                    localClose(s, PalLinkProtocol.R_RESET);
                    return;
                }
                if (f.payload.length == 0) return;
                if (!s.win.onDataReceived(f.payload.length)) {
                    Log.w(TAG, "stream " + f.stream + " overran its credit window");
                    localClose(s, PalLinkProtocol.R_RESET);
                    return;
                }
                s.out.offer(f.payload);
                return;
            }
            case PalLinkProtocol.T_CLOSE: {
                Stream s = streams.remove(f.stream);
                if (s == null) return;
                Log.i(TAG, "stream " + f.stream + " closed by device (reason "
                    + PalLinkProtocol.parseCloseReason(f.payload) + ")");
                s.remoteClose();
                publish();
                return;
            }
            case PalLinkProtocol.T_CREDIT: {
                Stream s = streams.get(f.stream);
                if (s == null) return;
                try {
                    s.win.addSendCredit(PalLinkProtocol.parseCredit(f.payload));
                } catch (PalLinkProtocol.MalformedFrameException e) {
                    Log.w(TAG, "bad CREDIT on stream " + f.stream);
                    localClose(s, PalLinkProtocol.R_RESET);
                }
                return;
            }
            case PalLinkProtocol.T_MODE: {
                try {
                    boolean busy = PalLinkProtocol.parseMode(f.payload) == PalLinkProtocol.MODE_BUSY;
                    Log.i(TAG, "MODE " + (busy ? "busy" : "idle"));
                    requestPriority(busy);
                } catch (PalLinkProtocol.MalformedFrameException e) {
                    Log.w(TAG, "bad MODE");
                }
                return;
            }
            default:
                Log.w(TAG, "unknown frame type 0x" + Integer.toHexString(f.type) + " ignored");
        }
    }

    private void handleOpen(final int id, byte[] payload) {
        if (id < 1 || id > maxStreams) {
            Log.w(TAG, "OPEN on invalid stream " + id);
            if (id != 0) sendFrame(PalLinkProtocol.close(id, PalLinkProtocol.R_REFUSED), "close");
            return;
        }
        Stream old = streams.remove(id);
        if (old != null) {
            Log.w(TAG, "OPEN on busy stream " + id + " — dropping the old one");
            old.abort();
        }
        PalLinkProtocol.Open open;
        try {
            open = PalLinkProtocol.parseOpen(payload);
        } catch (PalLinkProtocol.MalformedFrameException e) {
            Log.w(TAG, "bad OPEN on stream " + id + ": " + e.getMessage());
            sendFrame(PalLinkProtocol.close(id, PalLinkProtocol.R_REFUSED), "close");
            publish();
            return;
        }
        if (!PalLinkProtocol.isAllowedOpen(open.port, open.host, allowedHost)) {
            Log.w(TAG, "OPEN refused: " + open.host + ":" + open.port);
            sendFrame(PalLinkProtocol.close(id, PalLinkProtocol.R_REFUSED), "close");
            publish();
            return;
        }
        final Stream s = new Stream(id, open.host, open.port);
        streams.put(id, s);
        publish();
        s.startConnect();
    }

    /** Phone-side close (socket EOF/error, protocol violation): CLOSE the device. */
    private void localClose(Stream s, int reason) {
        if (streams.get(s.id) != s) return;   // already closed either way
        streams.remove(s.id);
        sendFrame(PalLinkProtocol.close(s.id, reason), "close");
        if (reason == PalLinkProtocol.R_NORMAL) s.remoteClose();   // flush queued device data first
        else s.abort();
        publish();
    }

    private void abortAllStreams() {
        if (streams.isEmpty()) return;
        Log.i(TAG, "link down — closing " + streams.size() + " stream(s)");
        for (Stream s : new ArrayList<>(streams.values())) s.abort();
        streams.clear();
    }

    // ---- one relayed TCP stream ---------------------------------------------

    private static final byte[] EOF = new byte[0];

    private final class Stream {
        final int id;
        final String host;
        final int port;
        final PalLinkProtocol.Window win = new PalLinkProtocol.Window();
        final LinkedBlockingQueue<byte[]> out = new LinkedBlockingQueue<>();
        final Socket sock = new Socket();
        volatile boolean opened = false;    // OPENED sent (link thread only writes)
        volatile boolean done = false;      // closed by either side
        private boolean writerStarted = false;

        Stream(int id, String host, int port) { this.id = id; this.host = host; this.port = port; }

        boolean isCurrent() { return !done && streams.get(id) == this; }

        void startConnect() {
            Thread t = new Thread(() -> {
                long t0 = SystemClock.elapsedRealtime();
                try {
                    sock.setTcpNoDelay(true);
                    sock.setKeepAlive(true);
                    sock.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
                } catch (Exception e) {
                    Log.w(TAG, "stream " + id + " connect to " + host + " failed: " + e);
                    closeQuietly();
                    h.post(() -> {
                        if (!isCurrent()) return;
                        streams.remove(id);
                        done = true;
                        win.close();
                        sendFrame(PalLinkProtocol.close(id, PalLinkProtocol.R_CONNECT_FAILED), "close");
                        publish();
                    });
                    return;
                }
                Log.i(TAG, "stream " + id + " connected in " + (SystemClock.elapsedRealtime() - t0) + " ms");
                h.post(this::onConnected);
            }, "pal-link-connect-" + id);
            t.setDaemon(true);
            t.start();
        }

        /** Link thread. */
        private void onConnected() {
            if (!isCurrent()) { closeQuietly(); return; }
            opened = true;
            sendFrame(PalLinkProtocol.opened(id), "opened");
            writerStarted = true;
            Thread r = new Thread(this::readLoop, "pal-link-rd-" + id);
            Thread w = new Thread(this::writeLoop, "pal-link-wr-" + id);
            r.setDaemon(true);
            w.setDaemon(true);
            r.start();
            w.start();
        }

        /** Internet → device. Blocks on credit, then on the socket. */
        private void readLoop() {
            try {
                InputStream in = sock.getInputStream();
                byte[] buf = new byte[PalLinkProtocol.maxDataPayload(REQUEST_MTU)];
                while (true) {
                    int max = win.awaitSendCredit(Math.min(buf.length, PalLinkProtocol.maxDataPayload(mtu)));
                    if (max < 0) return;
                    int n = in.read(buf, 0, max);
                    if (n < 0) {
                        h.post(() -> { if (isCurrent()) localClose(this, PalLinkProtocol.R_NORMAL); });
                        return;
                    }
                    if (n == 0) continue;
                    win.consumeSend(n);
                    final byte[] frame = PalLinkProtocol.data(id, buf, 0, n);
                    // Posted in order with the CLOSE above, so queued data goes out first.
                    h.post(() -> { if (isCurrent()) sendFrame(frame, "data"); });
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } catch (IOException e) {
                if (!done) {
                    Log.w(TAG, "stream " + id + " read error: " + e);
                    h.post(() -> { if (isCurrent()) localClose(this, PalLinkProtocol.R_RESET); });
                }
            }
        }

        /** Device → internet. Reports drained bytes so the link thread can grant credit. */
        private void writeLoop() {
            try {
                OutputStream os = sock.getOutputStream();
                while (true) {
                    byte[] b = out.take();
                    if (b == EOF) break;
                    os.write(b);
                    final int n = b.length;
                    h.post(() -> {
                        if (!isCurrent()) return;
                        long grant = win.onDrained(n);
                        if (grant > 0) sendFrame(PalLinkProtocol.credit(id, grant), "credit");
                    });
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } catch (IOException e) {
                if (!done) {
                    Log.w(TAG, "stream " + id + " write error: " + e);
                    h.post(() -> { if (isCurrent()) localClose(this, PalLinkProtocol.R_RESET); });
                }
            } finally {
                closeQuietly();
            }
        }

        /** Peer closed: deliver queued device data, then close the socket. */
        void remoteClose() {
            done = true;
            win.close();
            if (writerStarted) out.offer(EOF);
            else closeQuietly();
        }

        /** Link lost / reset: drop everything now. */
        void abort() {
            done = true;
            win.close();
            out.clear();
            out.offer(EOF);
            closeQuietly();
        }

        void closeQuietly() {
            try { sock.close(); } catch (IOException ignored) { }
        }
    }
}
