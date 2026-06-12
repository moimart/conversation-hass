import Foundation
import Capacitor
import AVFoundation

/// Native front-camera presence check for the "mirror" feature. The mirror's
/// camera feed itself is plain web getUserMedia in the WKWebView (granted via the
/// WKUIDelegate in MainViewController); this plugin only answers the gating
/// question "does this device have a front camera?" definitively and WITHOUT
/// triggering a permission prompt or opening the camera, so the UI can hide the
/// mirror button on devices that have none.
///
/// `hasFrontCamera()` resolves `{ present: Bool }`. Never rejects.
@objc(PalMirrorPlugin)
public class PalMirrorPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "PalMirrorPlugin"
    public let jsName = "PalMirror"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "hasFrontCamera", returnType: CAPPluginReturnPromise),
    ]

    @objc func hasFrontCamera(_ call: CAPPluginCall) {
        // Enumerate every front-position video device type (TrueDepth-only front
        // cameras don't match .builtInWideAngleCamera alone), and fall back to the
        // default front-camera lookup. Device discovery does not require camera
        // authorization, so this answers "does the hardware exist?" before any
        // permission prompt.
        var types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera, .builtInTrueDepthCamera]
        if #available(iOS 15.4, *) { types.append(.builtInLiDARDepthCamera) }
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: types,
            mediaType: .video,
            position: .front
        )
        let present = !session.devices.isEmpty
            || AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .front) != nil
        call.resolve(["present": present])
    }
}
