import SwiftUI

/// Spike UI: the PAL orb as the mic. Tap to listen (orb pulses, live partial
/// transcript below), auto-stops on silence, success haptic + final text.
/// The diagnostics line is the spike's verdict (framework / availability /
/// on-device support, then which engine actually transcribed).
struct ContentView: View {
    @StateObject private var speech = SpeechManager()
    @State private var pulsing = false

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                micControl
                    .frame(width: 90, height: 90)
                Text(statusLine)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                if !speech.transcript.isEmpty {
                    Text(speech.transcript)
                        .font(.footnote)
                        .multilineTextAlignment(.center)
                }
                if !speech.diagnostics.isEmpty {
                    Text(speech.diagnostics)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.cyan)
                        .multilineTextAlignment(.leading)
                }
            }
            .frame(maxWidth: .infinity)
        }
        .onAppear { speech.describeSupport() }
    }

    /// Path B (in-app recognizer): orb is a tap target driving SpeechManager.
    /// Path A (the watchOS reality): orb is the LABEL of a TextFieldLink — tap
    /// opens the system dictation sheet, the result lands in `dictated(_:)`.
    @ViewBuilder
    private var micControl: some View {
        if SpeechManager.pathBAvailable {
            orb.onTapGesture { speech.toggle() }
        } else {
            TextFieldLink(prompt: Text("PAL command")) {
                orb
            } onSubmit: { value in
                speech.dictated(value)
            }
            .buttonStyle(.plain)
        }
    }

    private var statusLine: String {
        switch speech.phase {
        case .idle: return "tap to speak"
        case .requesting: return "permissions…"
        case .listening: return "listening — tap to stop"
        case .done: return "tap to speak again"
        case .error(let message): return message
        }
    }

    private var orb: some View {
        ZStack {
            Circle()
                .fill(
                    RadialGradient(
                        colors: orbColors,
                        center: .center,
                        startRadius: 4,
                        endRadius: 48
                    )
                )
                .shadow(color: orbColors[0].opacity(0.7), radius: pulsing ? 18 : 8)
                .scaleEffect(speech.phase == .listening && pulsing ? 1.08 : 1.0)
            Circle()
                .stroke(orbColors[0].opacity(0.5), lineWidth: 2)
        }
        .animation(
            speech.phase == .listening
                ? .easeInOut(duration: 0.7).repeatForever(autoreverses: true)
                : .default,
            value: pulsing
        )
        .onChange(of: speech.phase) { _, newPhase in
            pulsing = (newPhase == .listening)
        }
    }

    private var orbColors: [Color] {
        switch speech.phase {
        case .listening: return [.cyan, .blue.opacity(0.3)]
        case .done: return [.green, .teal.opacity(0.3)]
        case .error: return [.orange, .red.opacity(0.3)]
        default: return [.teal, .blue.opacity(0.25)]
        }
    }
}
