import UIKit
import Capacitor

/// Capacitor 8 does NOT auto-load app-embedded Swift plugins (only packaged
/// ones via SPM). Our native LAN-discovery bridge lives in the App target, so we
/// register it explicitly here. Main.storyboard points its root view controller
/// at this class instead of the stock CAPBridgeViewController.
class MainViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(PalDiscoveryPlugin())
    }
}
