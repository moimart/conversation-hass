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
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInWideAngleCamera],
            mediaType: .video,
            position: .front
        )
        call.resolve(["present": !session.devices.isEmpty])
    }
}
