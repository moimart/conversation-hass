package sh.martinez.pal.companion;

import android.content.Context;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Native front-camera presence check for the "mirror" feature. The mirror's
 * camera feed itself is plain web getUserMedia in the WebView (Capacitor's
 * BridgeWebChromeClient grants it at runtime); this plugin only answers the
 * gating question "does this device have a front camera?" — without opening the
 * camera or prompting — so the UI can hide the mirror button when there is none.
 *
 * hasFrontCamera() resolves { present: boolean }. Never rejects.
 */
@CapacitorPlugin(name = "PalMirror")
public class PalMirrorPlugin extends Plugin {

    @PluginMethod
    public void hasFrontCamera(PluginCall call) {
        boolean present = false;
        try {
            CameraManager manager =
                (CameraManager) getContext().getSystemService(Context.CAMERA_SERVICE);
            if (manager != null) {
                for (String id : manager.getCameraIdList()) {
                    Integer facing = manager.getCameraCharacteristics(id)
                        .get(CameraCharacteristics.LENS_FACING);
                    if (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT) {
                        present = true;
                        break;
                    }
                }
            }
        } catch (Exception e) {
            present = false;   // camera service unavailable → treat as no front cam
        }
        JSObject ret = new JSObject();
        ret.put("present", present);
        call.resolve(ret);
    }
}
