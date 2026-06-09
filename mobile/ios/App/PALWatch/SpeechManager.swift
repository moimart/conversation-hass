import AVFoundation
import Foundation
import SwiftUI
import WatchKit
#if canImport(Speech)
import Speech
#endif

/// On-device dictation loop for the PTT spike (plan "Path B": custom
/// recognizer so the orb stays on screen as the mic, instead of the system
/// dictation sheet).
///
/// The spike's whole purpose is answering: does SFSpeechRecognizer with
/// requiresOnDeviceRecognition work on this physical watch? So besides the
/// listen loop, `describeSupport()` surfaces the verdict on screen, and the
/// entire file compiles (and the app installs) even if the Speech framework
/// is absent on watchOS — `#if canImport(Speech)` collapses it to a
/// diagnostics-only stub in that case, which is itself the spike answer.
@MainActor
final class SpeechManager: NSObject, ObservableObject {
    enum Phase: Equatable {
        case idle
        case requesting
        case listening
        case done
        case error(String)
    }

    /// Whether Path B (custom in-app recognizer) exists on this platform.
    /// SPIKE VERDICT 2026-06-09: false — the watchOS SDK (26.4) ships no
    /// Speech framework at all, so the UI must use Path A (TextFieldLink →
    /// system dictation sheet) instead.
#if canImport(Speech)
    static let pathBAvailable = true
#else
    static let pathBAvailable = false
#endif

    @Published var phase: Phase = .idle
    @Published var transcript = ""
    @Published var diagnostics = ""

    /// Auto-endpoint: stop after this much silence once words have arrived.
    private let silenceWindow: TimeInterval = 1.3
    /// Give up if nothing at all was heard for this long.
    private let emptyTimeout: TimeInterval = 6.0

#if canImport(Speech)
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var endpointTimer: Timer?
    private var lastChange = Date()
    private var usedOnDevice = false

    func describeSupport() {
        var lines: [String] = []
        lines.append("Speech framework: present")
        if let recognizer {
            lines.append("recognizer(en-US): ok")
            lines.append("available: \(recognizer.isAvailable)")
            lines.append("on-device: \(recognizer.supportsOnDeviceRecognition)")
        } else {
            lines.append("recognizer(en-US): nil")
        }
        diagnostics = lines.joined(separator: "\n")
    }

    func toggle() {
        if phase == .listening {
            endpoint()
        } else {
            start()
        }
    }

    /// Path A result hand-off (unused when Path B drives the orb, but both
    /// ContentView branches are type-checked regardless of the runtime flag).
    func dictated(_ text: String) {
        transcript = text
        phase = text.isEmpty ? .error("heard nothing") : .done
    }

    func start() {
        transcript = ""
        phase = .requesting
        SFSpeechRecognizer.requestAuthorization { [weak self] auth in
            Task { @MainActor in
                guard let self else { return }
                guard auth == .authorized else {
                    self.phase = .error("speech permission denied (\(auth.rawValue))")
                    return
                }
                self.requestMic()
            }
        }
    }

    private func requestMic() {
        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            Task { @MainActor in
                guard let self else { return }
                guard granted else {
                    self.phase = .error("mic permission denied")
                    return
                }
                self.beginRecognition()
            }
        }
    }

    private func beginRecognition() {
        guard let recognizer else {
            phase = .error("no recognizer for en-US")
            return
        }
        guard recognizer.isAvailable else {
            phase = .error("recognizer unavailable")
            return
        }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .default)
            try session.setActive(true, options: .notifyOthersOnDeactivation)

            let req = SFSpeechAudioBufferRecognitionRequest()
            req.shouldReportPartialResults = true
            // Spike: prefer on-device; record which engine actually ran so the
            // accuracy verdict is attributed correctly.
            usedOnDevice = recognizer.supportsOnDeviceRecognition
            req.requiresOnDeviceRecognition = usedOnDevice
            request = req

            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
                req.append(buffer)
            }
            engine.prepare()
            try engine.start()

            lastChange = Date()
            phase = .listening
            armEndpointTimer()

            task = recognizer.recognitionTask(with: req) { [weak self] result, error in
                Task { @MainActor in
                    guard let self, self.phase == .listening else { return }
                    if let result {
                        self.transcript = result.bestTranscription.formattedString
                        self.lastChange = Date()
                        if result.isFinal {
                            self.endpoint()
                        }
                    } else if error != nil {
                        self.endpoint()
                    }
                }
            }
        } catch {
            phase = .error("audio: \(error.localizedDescription)")
            teardownAudio()
        }
    }

    private func armEndpointTimer() {
        endpointTimer?.invalidate()
        endpointTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.phase == .listening else { return }
                let quiet = Date().timeIntervalSince(self.lastChange)
                let limit = self.transcript.isEmpty ? self.emptyTimeout : self.silenceWindow
                if quiet > limit {
                    self.endpoint()
                }
            }
        }
    }

    /// Stop listening and settle on whatever was transcribed.
    private func endpoint() {
        guard phase == .listening else { return }
        phase = transcript.isEmpty ? .error("heard nothing") : .done
        if phase == .done {
            WKInterfaceDevice.current().play(.success)
            diagnostics = "engine: \(usedOnDevice ? "ON-DEVICE" : "server-based")"
        }
        teardownAudio()
    }

    private func teardownAudio() {
        endpointTimer?.invalidate()
        endpointTimer = nil
        task?.cancel()
        task = nil
        request?.endAudio()
        request = nil
        if engine.isRunning {
            engine.stop()
        }
        engine.inputNode.removeTap(onBus: 0)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
#else
    // Speech framework absent on this SDK (the verified watchOS reality) —
    // ContentView routes the orb through Path A (TextFieldLink → system
    // dictation) and reports results back via `dictated(_:)`.
    func describeSupport() {
        diagnostics = "Speech fw: absent on watchOS → Path A (system dictation)"
    }

    func toggle() {
        phase = .error("use the dictation orb")
    }

    /// Path A result hand-off from the system dictation sheet.
    func dictated(_ text: String) {
        transcript = text
        if text.isEmpty {
            phase = .error("heard nothing")
        } else {
            phase = .done
            WKInterfaceDevice.current().play(.success)
        }
    }
#endif
}
