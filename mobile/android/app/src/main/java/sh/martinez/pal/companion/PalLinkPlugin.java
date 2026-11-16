package sh.martinez.pal.companion;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;
import android.util.Log;

import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * JS API for the PAL Round BLE link (Android only; see PalLinkService and
 * firmware/pal-round/BLE_LINK.md).
 *
 *   pair({gatewayHost?})  scan for the link service (~20 s), connect, createBond()
 *                         (Android shows the passkey dialog), store the device;
 *                         with gatewayHost also enables + starts the relay.
 *   cancelPair()
 *   unpair()              stop, forget the device, best-effort removeBond.
 *   start({gatewayHost})  enable the relay (persisted) and start the service.
 *   stop()                disable the relay.
 *   status()              {enabled, paired, deviceName, connected, streams, gatewayHost}
 *   events: statusChange (same shape as status), pairState {state: scanning|bonding}
 *
 * Scanning happens ONLY inside pair().
 */
@SuppressLint("MissingPermission")   // permissions are checked/requested before BT use
@CapacitorPlugin(
    name = "PalLink",
    permissions = {
        @Permission(alias = "bluetooth", strings = {
            "android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT" }),
        @Permission(alias = "location", strings = { "android.permission.ACCESS_FINE_LOCATION" }),
        @Permission(alias = "notifications", strings = { "android.permission.POST_NOTIFICATIONS" }),
    }
)
public class PalLinkPlugin extends Plugin {

    private static final String TAG = PalLinkService.TAG;
    private static final long SCAN_TIMEOUT_MS = 20_000;
    private static final long BOND_TIMEOUT_MS = 120_000;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final PalLinkState.Listener stateListener = () ->
        main.post(() -> notifyListeners("statusChange", statusObj()));

    // ---- pairing session (main thread) -------------------------------------
    private PluginCall pairCall;
    private String pairGatewayHost;
    private BluetoothLeScanner scanner;
    private ScanCallback scanCb;
    private BluetoothGatt pairGatt;
    private BluetoothDevice pairDevice;
    private String pairName;
    private boolean bondRequested;
    private boolean bondReceiverRegistered;
    private final Runnable scanTimeout = () -> failPair("No PAL Round found. Is it in pairing mode?");
    private final Runnable bondTimeout = () -> failPair("Pairing timed out");

    @Override
    public void load() {
        PalLinkState.addListener(stateListener);
        // App launch: bring the relay back if the user left it on.
        PalLinkState.startIfEnabled(getContext());
    }

    @Override
    protected void handleOnDestroy() {
        PalLinkState.removeListener(stateListener);
        cleanupPair();
    }

    // ---- permissions ---------------------------------------------------------

    private boolean granted(String perm) {
        return ContextCompat.checkSelfPermission(getContext(), perm) == PackageManager.PERMISSION_GRANTED;
    }

    /** Required for BLE on this OS version (scan only matters for pair()). */
    private boolean hasBlePerms(boolean scan) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return granted("android.permission.BLUETOOTH_CONNECT")
                && (!scan || granted("android.permission.BLUETOOTH_SCAN"));
        }
        return !scan || granted("android.permission.ACCESS_FINE_LOCATION");
    }

    /** Aliases still worth prompting for. Notifications are nice-to-have. */
    private String[] missingAliases(boolean scan) {
        List<String> a = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (!hasBlePerms(scan)) a.add("bluetooth");
        } else if (scan && !hasBlePerms(true)) {
            a.add("location");
        }
        if (Build.VERSION.SDK_INT >= 33 && !granted("android.permission.POST_NOTIFICATIONS")) {
            a.add("notifications");
        }
        return a.toArray(new String[0]);
    }

    // ---- pair ------------------------------------------------------------------

    @PluginMethod
    public void pair(PluginCall call) {
        if (pairCall != null) { call.reject("Pairing already in progress"); return; }
        String[] missing = missingAliases(true);
        if (missing.length > 0) {
            requestPermissionForAliases(missing, call, "pairPermsCallback");
            return;
        }
        doPair(call);
    }

    @PermissionCallback
    private void pairPermsCallback(PluginCall call) {
        if (!hasBlePerms(true)) { call.reject("Bluetooth permission denied"); return; }
        if (pairCall != null) { call.reject("Pairing already in progress"); return; }
        doPair(call);
    }

    private BluetoothAdapter adapter() {
        BluetoothManager bm = getContext().getSystemService(BluetoothManager.class);
        return bm != null ? bm.getAdapter() : null;
    }

    private void doPair(PluginCall call) {
        BluetoothAdapter ad = adapter();
        if (ad == null) { call.reject("This phone has no Bluetooth"); return; }
        if (!ad.isEnabled()) { call.reject("Bluetooth is off — turn it on and try again"); return; }
        scanner = ad.getBluetoothLeScanner();
        if (scanner == null) { call.reject("Bluetooth LE scanning unavailable"); return; }

        pairCall = call;
        String host = call.getString("gatewayHost", "");
        pairGatewayHost = host != null ? host.trim() : "";
        bondRequested = false;

        ScanFilter filter = new ScanFilter.Builder()
            .setServiceUuid(new ParcelUuid(PalLinkProtocol.SERVICE_UUID)).build();
        ScanSettings settings = new ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        scanCb = new ScanCallback() {
            @Override
            public void onScanResult(int callbackType, ScanResult result) {
                main.post(() -> onFound(result));
            }
            @Override
            public void onBatchScanResults(List<ScanResult> results) {
                if (results != null && !results.isEmpty()) main.post(() -> onFound(results.get(0)));
            }
            @Override
            public void onScanFailed(int errorCode) {
                main.post(() -> failPair("Bluetooth scan failed (" + errorCode + ")"));
            }
        };
        try {
            scanner.startScan(Collections.singletonList(filter), settings, scanCb);
        } catch (Exception e) {
            scanCb = null;
            failPair("Couldn't start scanning: " + e.getMessage());
            return;
        }
        Log.i(TAG, "pair: scanning");
        emitPairState("scanning");
        main.postDelayed(scanTimeout, SCAN_TIMEOUT_MS);
    }

    private void stopScan() {
        main.removeCallbacks(scanTimeout);
        if (scanner != null && scanCb != null) {
            try { scanner.stopScan(scanCb); } catch (Exception ignored) { }
        }
        scanCb = null;
    }

    private void onFound(ScanResult result) {
        if (pairCall == null || pairDevice != null) return;   // already handling one
        stopScan();
        pairDevice = result.getDevice();
        String name = result.getScanRecord() != null ? result.getScanRecord().getDeviceName() : null;
        if (name == null) name = pairDevice.getName();
        pairName = name != null ? name : "PAL Round";
        Log.i(TAG, "pair: found " + pairName + " " + pairDevice.getAddress());

        IntentFilter f = new IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED);
        ContextCompat.registerReceiver(getContext(), bondReceiver, f, ContextCompat.RECEIVER_NOT_EXPORTED);
        bondReceiverRegistered = true;
        main.postDelayed(bondTimeout, BOND_TIMEOUT_MS);

        if (pairDevice.getBondState() == BluetoothDevice.BOND_BONDED) {
            // Already bonded at the OS level (e.g. re-pair after Forget in the app).
            succeedPair();
            return;
        }
        emitPairState("bonding");
        pairGatt = pairDevice.connectGatt(getContext(), false, new BluetoothGattCallback() {
            @Override
            public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
                main.post(() -> onPairConn(g, status, newState));
            }
        }, BluetoothDevice.TRANSPORT_LE);
        if (pairGatt == null) failPair("Couldn't connect to the PAL Round");
    }

    private void onPairConn(BluetoothGatt g, int status, int newState) {
        if (g != pairGatt || pairCall == null) return;
        if (newState == BluetoothProfile.STATE_CONNECTED) {
            if (bondRequested) return;
            bondRequested = true;
            Log.i(TAG, "pair: connected, createBond()");
            if (!pairDevice.createBond()) failPair("Couldn't start pairing");
        } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
            int bs = pairDevice.getBondState();
            if (bs == BluetoothDevice.BOND_BONDED) { succeedPair(); return; }
            if (bs == BluetoothDevice.BOND_BONDING) return;   // the bond broadcast decides
            failPair("The PAL Round dropped the connection. Is it in pairing mode?");
        }
    }

    private final BroadcastReceiver bondReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context ctx, Intent intent) {
            BluetoothDevice d = intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE);
            if (d == null || pairDevice == null || !d.getAddress().equals(pairDevice.getAddress())) return;
            int state = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR);
            int prev = intent.getIntExtra(BluetoothDevice.EXTRA_PREVIOUS_BOND_STATE, BluetoothDevice.ERROR);
            Log.i(TAG, "pair: bond state " + prev + " → " + state);
            if (state == BluetoothDevice.BOND_BONDED) succeedPair();
            else if (state == BluetoothDevice.BOND_NONE && prev == BluetoothDevice.BOND_BONDING) {
                failPair("Pairing failed or was cancelled");
            }
        }
    };

    private void emitPairState(String state) {
        JSObject o = new JSObject();
        o.put("state", state);
        notifyListeners("pairState", o);
    }

    private void succeedPair() {
        PluginCall call = pairCall;
        if (call == null) return;
        String addr = pairDevice.getAddress();
        String name = pairName;
        String host = pairGatewayHost;
        cleanupPair();
        android.content.SharedPreferences.Editor e = PalLinkState.prefs(getContext()).edit()
            .putString(PalLinkState.KEY_ADDRESS, addr)
            .putString(PalLinkState.KEY_NAME, name);
        if (host != null && !host.isEmpty()) {
            e.putBoolean(PalLinkState.KEY_ENABLED, true).putString(PalLinkState.KEY_HOST, host);
        }
        e.apply();
        Log.i(TAG, "pair: bonded with " + name + " " + addr);
        // A running service re-targets the new address in onStartCommand.
        PalLinkState.startIfEnabled(getContext());
        PalLinkState.notifyChanged();
        call.resolve(statusObj());
    }

    private void failPair(String msg) {
        PluginCall call = pairCall;
        if (call == null) return;
        Log.w(TAG, "pair failed: " + msg);
        cleanupPair();
        call.reject(msg);
    }

    private void cleanupPair() {
        stopScan();
        main.removeCallbacks(bondTimeout);
        if (bondReceiverRegistered) {
            try { getContext().unregisterReceiver(bondReceiver); } catch (Exception ignored) { }
            bondReceiverRegistered = false;
        }
        if (pairGatt != null) {
            try { pairGatt.disconnect(); pairGatt.close(); } catch (Exception ignored) { }
            pairGatt = null;
        }
        pairCall = null;
        pairDevice = null;
        pairName = null;
        bondRequested = false;
    }

    @PluginMethod
    public void cancelPair(PluginCall call) {
        failPair("Cancelled");
        call.resolve();
    }

    // ---- unpair / start / stop / status --------------------------------------

    @PluginMethod
    public void unpair(PluginCall call) {
        Context ctx = getContext();
        String addr = PalLinkState.address(ctx);
        PalLinkService.stop(ctx);
        PalLinkState.prefs(ctx).edit()
            .remove(PalLinkState.KEY_ADDRESS)
            .remove(PalLinkState.KEY_NAME)
            .putBoolean(PalLinkState.KEY_ENABLED, false)
            .apply();
        if (addr != null && hasBlePerms(false)) {
            try {
                BluetoothAdapter ad = adapter();
                if (ad != null) {
                    BluetoothDevice d = ad.getRemoteDevice(addr);
                    Method m = d.getClass().getMethod("removeBond");
                    m.invoke(d);
                }
            } catch (Exception e) {
                Log.w(TAG, "removeBond failed (forget it in Bluetooth settings)", e);
            }
        }
        PalLinkState.notifyChanged();
        call.resolve(statusObj());
    }

    @PluginMethod
    public void start(PluginCall call) {
        String host = call.getString("gatewayHost", "");
        if (host == null || host.trim().isEmpty()) { call.reject("gatewayHost required"); return; }
        String[] missing = missingAliases(false);
        if (missing.length > 0) {
            requestPermissionForAliases(missing, call, "startPermsCallback");
            return;
        }
        doStart(call);
    }

    @PermissionCallback
    private void startPermsCallback(PluginCall call) {
        if (!hasBlePerms(false)) { call.reject("Bluetooth permission denied"); return; }
        doStart(call);
    }

    private void doStart(PluginCall call) {
        Context ctx = getContext();
        String host = call.getString("gatewayHost", "").trim();
        PalLinkState.prefs(ctx).edit()
            .putBoolean(PalLinkState.KEY_ENABLED, true)
            .putString(PalLinkState.KEY_HOST, host)
            .apply();
        PalLinkState.startIfEnabled(ctx);   // no-op until paired
        PalLinkState.notifyChanged();
        call.resolve(statusObj());
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Context ctx = getContext();
        PalLinkState.prefs(ctx).edit().putBoolean(PalLinkState.KEY_ENABLED, false).apply();
        PalLinkService.stop(ctx);
        PalLinkState.notifyChanged();
        call.resolve(statusObj());
    }

    @PluginMethod
    public void status(PluginCall call) {
        call.resolve(statusObj());
    }

    private JSObject statusObj() {
        Context ctx = getContext();
        JSObject o = new JSObject();
        o.put("supported", true);
        o.put("enabled", PalLinkState.isEnabled(ctx));
        o.put("paired", PalLinkState.isPaired(ctx));
        o.put("deviceName", PalLinkState.deviceName(ctx));
        o.put("gatewayHost", PalLinkState.gatewayHost(ctx));
        o.put("connected", PalLinkState.isLinkUp());
        o.put("streams", PalLinkState.streamCount());
        o.put("permission", hasBlePerms(false));
        return o;
    }
}
