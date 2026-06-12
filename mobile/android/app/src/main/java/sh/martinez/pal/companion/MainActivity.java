package sh.martinez.pal.companion;

import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.PermissionRequest;
import android.webkit.WebView;

import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebChromeClient;

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
        // App-embedded Capacitor plugins must be registered before the bridge is
        // created (i.e. before super.onCreate). PalMirror gates the front-camera
        // "mirror" button by reporting whether a front camera exists.
        registerPlugin(PalMirrorPlugin.class);
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
        // insets so the WebView fills truly edge-to-edge; for the on-screen
        // keyboard, SHRINK the WebView (bottom margin) rather than pad it —
        // padding a Chromium WebView doesn't reflow its layout viewport, so a
        // `position:fixed; bottom:0` input stays hidden behind the keyboard.
        // A real height change makes the WebView re-measure and the input rides
        // up above the keyboard.
        final View web = getBridge().getWebView();
        if (web instanceof WebView) {
            // getUserMedia in the WebView (intercom calls, mirror) needs the
            // web-layer capture permission GRANTED — the default Capacitor chrome
            // client leaves audio capture denied (NotAllowedError), so we grant
            // camera/mic requests here. The OS-level runtime perms (CAMERA /
            // RECORD_AUDIO) are declared in the manifest and prompted separately.
            ((WebView) web).setWebChromeClient(new BridgeWebChromeClient(getBridge()) {
                @Override
                public void onPermissionRequest(final PermissionRequest request) {
                    runOnUiThread(() -> request.grant(request.getResources()));
                }
            });
        }
        if (web != null) {
            ViewCompat.setOnApplyWindowInsetsListener(web, (v, insets) -> {
                int imeBottom = insets.getInsets(WindowInsetsCompat.Type.ime()).bottom;
                ViewGroup.LayoutParams params = v.getLayoutParams();
                if (params instanceof ViewGroup.MarginLayoutParams) {
                    ViewGroup.MarginLayoutParams mlp = (ViewGroup.MarginLayoutParams) params;
                    if (mlp.bottomMargin != imeBottom) {
                        mlp.bottomMargin = imeBottom;
                        v.setLayoutParams(mlp);
                    }
                }
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
