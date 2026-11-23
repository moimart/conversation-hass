package sh.martinez.pal.companion;

import android.content.Intent;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Bridge between the app and its home-screen widgets.
 *
 *   configure({home, gateway, token})  where the widget fetches notes from; refreshes them
 *   clear()                             forget it (unpaired)
 *   refresh()                           refresh now (a note was recorded, the app went away)
 *   takeLaunchAction() → {action}       "notes" if the app was opened from the widget, once
 *   "launchAction" event                the same, when the running app is brought up by it
 */
@CapacitorPlugin(name = "PalWidget")
public class PalWidgetPlugin extends Plugin {
    private String pendingAction = "";

    @Override
    public void load() {
        if (getActivity() != null) take(getActivity().getIntent());
    }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        super.handleOnNewIntent(intent);
        if (take(intent)) {
            JSObject ev = new JSObject();
            ev.put("action", pendingAction);
            pendingAction = "";
            notifyListeners("launchAction", ev);
        }
    }

    @Override
    protected void handleOnPause() {
        super.handleOnPause();
        NotesWidgetProvider.refreshAll(getContext());
    }

    private boolean take(Intent intent) {
        if (intent == null) return false;
        String a = intent.getStringExtra(NotesWidgetProvider.EXTRA_OPEN);
        if (a == null || a.isEmpty()) return false;
        intent.removeExtra(NotesWidgetProvider.EXTRA_OPEN);   // not again on recreate
        pendingAction = a;
        return true;
    }

    @PluginMethod
    public void configure(PluginCall call) {
        PalWidgetStore.save(getContext(), call.getString("home", ""), call.getString("gateway", ""),
            call.getString("token", ""));
        NotesWidgetProvider.refreshAll(getContext());
        call.resolve();
    }

    @PluginMethod
    public void clear(PluginCall call) {
        PalWidgetStore.clear(getContext());
        NotesWidgetProvider.refreshAll(getContext());
        call.resolve();
    }

    @PluginMethod
    public void refresh(PluginCall call) {
        NotesWidgetProvider.refreshAll(getContext());
        call.resolve();
    }

    @PluginMethod
    public void takeLaunchAction(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("action", pendingAction);
        pendingAction = "";
        call.resolve(ret);
    }
}
