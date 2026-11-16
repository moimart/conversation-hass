package sh.martinez.pal.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Bring the PAL Round relay back after a reboot / app update, if the user left it on. */
public class PalLinkBootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        String a = intent != null ? intent.getAction() : null;
        if (Intent.ACTION_BOOT_COMPLETED.equals(a) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            PalLinkState.startIfEnabled(ctx);
        }
    }
}
