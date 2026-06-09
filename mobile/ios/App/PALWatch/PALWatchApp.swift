import SwiftUI

@main
struct PALWatchApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}

/// Routes between one-time enrollment and the PTT orb based on paired state.
struct RootView: View {
    @State private var paired = ConfigStore.isPaired

    var body: some View {
        if paired {
            ContentView()
        } else {
            EnrollView(defaultLan: "http://10.20.30.185:8765") { paired = true }
        }
    }
}
