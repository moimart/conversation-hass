package sh.martinez.pal.companion;

import android.os.Bundle;
import android.view.View;
import android.view.WindowManager;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;

/**
 * Full-screen immersive, the way games do it: hide BOTH system bars (status +
 * navigation), draw into the display cutout, and re-assert on every focus gain
 * (the bars otherwise return after dialogs / the keyboard / resume). Done
 * natively because the Capacitor status-bar plugin only recolors or insets the
 * bars — it can't remove them.
 */
public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Draw edge-to-edge, including INTO the camera cutout (must call
        // setAttributes() to actually apply the layout mode, not just mutate
        // the returned params).
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.layoutInDisplayCutoutMode =
            WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS;
        getWindow().setAttributes(lp);

        // Capacitor insets the WebView below the status/cutout area, leaving the
        // window background visible there. Consume the system-bar + cutout
        // insets so the WebView fills truly edge-to-edge; keep only a bottom
        // pad for the on-screen keyboard.
        final View web = getBridge().getWebView();
        if (web != null) {
            ViewCompat.setOnApplyWindowInsetsListener(web, (v, insets) -> {
                Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
                v.setPadding(0, 0, 0, ime.bottom);
                return WindowInsetsCompat.CONSUMED;
            });
            ViewCompat.requestApplyInsets(web);
        }
        applyImmersive();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) applyImmersive();
    }

    private void applyImmersive() {
        WindowInsetsControllerCompat controller =
            WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        controller.hide(WindowInsetsCompat.Type.systemBars());
        controller.setSystemBarsBehavior(
            WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
    }
}
