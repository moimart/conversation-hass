package sh.martinez.pal.companion;

import android.Manifest;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;

import androidx.core.content.ContextCompat;

import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Persisted PAL Round link settings (SharedPreferences) + live, process-local
 * link status shared between {@link PalLinkService} and {@link PalLinkPlugin}.
 */
final class PalLinkState {

    private PalLinkState() {}

    static final String PREFS = "pal_link";
    static final String KEY_ENABLED = "enabled";
    static final String KEY_ADDRESS = "address";
    static final String KEY_NAME = "device_name";
    static final String KEY_HOST = "gateway_host";

    interface Listener { void onChange(); }

    private static volatile boolean connected = false;
    private static volatile boolean linkUp = false;
    private static volatile int streams = 0;
    private static final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();

    static SharedPreferences prefs(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean isEnabled(Context ctx) { return prefs(ctx).getBoolean(KEY_ENABLED, false); }
    static String address(Context ctx) { return prefs(ctx).getString(KEY_ADDRESS, null); }
    static String deviceName(Context ctx) { return prefs(ctx).getString(KEY_NAME, null); }
    static String gatewayHost(Context ctx) { return prefs(ctx).getString(KEY_HOST, null); }
    static boolean isPaired(Context ctx) { return address(ctx) != null; }

    /** Runtime permission needed to talk GATT (Android 12+), always true before. */
    static boolean hasConnectPermission(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true;
        return ContextCompat.checkSelfPermission(ctx, Manifest.permission.BLUETOOTH_CONNECT)
            == PackageManager.PERMISSION_GRANTED;
    }

    static boolean shouldRun(Context ctx) {
        String host = gatewayHost(ctx);
        return isEnabled(ctx) && isPaired(ctx) && host != null && !host.isEmpty()
            && hasConnectPermission(ctx);
    }

    /** Start the relay service if the user enabled it (app launch / boot). */
    static void startIfEnabled(Context ctx) {
        if (!shouldRun(ctx)) return;
        try {
            PalLinkService.start(ctx);
        } catch (Exception e) {
            // e.g. ForegroundServiceStartNotAllowedException from the background.
            android.util.Log.w(PalLinkService.TAG, "couldn't start link service", e);
        }
    }

    static boolean isConnected() { return connected; }
    static boolean isLinkUp() { return linkUp; }
    static int streamCount() { return streams; }

    static void setLive(boolean isConnected, boolean isLinkUp, int streamCount) {
        boolean changed = connected != isConnected || linkUp != isLinkUp || streams != streamCount;
        connected = isConnected;
        linkUp = isLinkUp;
        streams = streamCount;
        if (changed) notifyChanged();
    }

    static void notifyChanged() {
        for (Listener l : listeners) {
            try { l.onChange(); } catch (Exception ignored) { /* never let a listener kill the link */ }
        }
    }

    static void addListener(Listener l) { listeners.add(l); }
    static void removeListener(Listener l) { listeners.remove(l); }
}
