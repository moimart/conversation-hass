package sh.martinez.pal.companion;

import android.content.Intent;
import android.content.pm.PackageManager;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Detect + launch other installed apps, so the UI can offer a shortcut (e.g. the
 * Home Assistant companion app) only when it's actually present. Mirrors the
 * PalMirror gating pattern.
 *
 * Package visibility (Android 11+): both methods rely on getLaunchIntentForPackage,
 * which only sees a package the app has declared in a <queries> manifest block —
 * see AndroidManifest.xml. Without that, isInstalled() reports false even when the
 * app is installed.
 *
 * isInstalled({packageName}) resolves { installed: boolean } and never rejects.
 * openApp({packageName}) launches it (or rejects if not launchable).
 */
@CapacitorPlugin(name = "PalApps")
public class PalAppsPlugin extends Plugin {

    @PluginMethod
    public void isInstalled(PluginCall call) {
        String pkg = call.getString("packageName", "");
        boolean installed = false;
        try {
            PackageManager pm = getContext().getPackageManager();
            installed = pkg != null && !pkg.isEmpty()
                && pm.getLaunchIntentForPackage(pkg) != null;
        } catch (Exception e) {
            installed = false;
        }
        JSObject ret = new JSObject();
        ret.put("installed", installed);
        call.resolve(ret);
    }

    @PluginMethod
    public void openApp(PluginCall call) {
        String pkg = call.getString("packageName", "");
        if (pkg == null || pkg.isEmpty()) {
            call.reject("packageName required");
            return;
        }
        try {
            Intent intent = getContext().getPackageManager().getLaunchIntentForPackage(pkg);
            if (intent == null) {
                call.reject("not installed");
                return;
            }
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            call.resolve();
        } catch (Exception e) {
            call.reject("launch failed: " + e.getMessage());
        }
    }
}
