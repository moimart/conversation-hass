import UIKit
import WebKit
import Capacitor

/// Capacitor 8 does NOT auto-load app-embedded Swift plugins (only packaged
/// ones via SPM). Our native bridges live in the App target, so we register them
/// explicitly here. Main.storyboard points its root view controller at this
/// class instead of the stock CAPBridgeViewController.
///
/// We also act as the web view's `WKUIDelegate`: WKWebView does not grant
/// getUserMedia capture on its own, so without this the front-camera "mirror"
/// feed would silently stay black even with NSCameraUsageDescription set. We
/// grant capture requests (the NSCameraUsageDescription prompt still gates the
/// first access at the OS level).
class MainViewController: CAPBridgeViewController, WKUIDelegate {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(PalDiscoveryPlugin())
        bridge?.registerPluginInstance(PalMirrorPlugin())
        webView?.uiDelegate = self
    }

    @available(iOS 15.0, *)
    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(.grant)
    }
}
