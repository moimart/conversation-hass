package sh.martinez.pal.companion.wear

import android.app.Application
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.launch

/**
 * Wear OS PTT spike — answers the #1 unknown: does IN-APP live recognition
 * (Path B: orb-as-mic, live partial transcript) work on the Pixel Watch 3?
 * Unlike watchOS (no Speech framework at all), Android exposes
 * android.speech.SpeechRecognizer + on-device APIs — this measures whether
 * they're actually backed on the watch.
 *
 * describeSupport() surfaces the verdict on screen. startPathB() drives the
 * in-app recognizer (preferring the on-device one). The ACTION_RECOGNIZE_SPEECH
 * intent (Path A, guaranteed) is the fallback the Activity wires to the orb if
 * Path B is unavailable.
 */
class SpeechManager(app: Application) : AndroidViewModel(app) {

    enum class Phase { IDLE, LISTENING, SENDING, DONE, ERROR }

    var phase by mutableStateOf(Phase.IDLE)
    var transcript by mutableStateOf("")
    var reply by mutableStateOf("")
    var diagnostics by mutableStateOf("")
    var usedOnDevice by mutableStateOf(false)

    private var recognizer: SpeechRecognizer? = null

    private val ctx get() = getApplication<Application>()

    val pathBAvailable: Boolean
        get() = SpeechRecognizer.isRecognitionAvailable(ctx)

    private val onDeviceAvailable: Boolean
        get() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx)

    fun describeSupport() {
        diagnostics = buildString {
            append("SpeechRecognizer avail: ${SpeechRecognizer.isRecognitionAvailable(ctx)}\n")
            append("on-device avail: $onDeviceAvailable\n")
            append("sdk: ${Build.VERSION.SDK_INT}  → ")
            append(if (pathBAvailable) "Path B (in-app)" else "Path A (intent)")
        }
    }

    /** Path B: in-app recognition with live partials. Returns false if no
     * SpeechRecognizer backend exists (caller should fall back to the intent). */
    fun startPathB(): Boolean {
        if (!pathBAvailable) return false
        transcript = ""
        phase = Phase.LISTENING
        recognizer?.destroy()
        recognizer = if (onDeviceAvailable) {
            usedOnDevice = true
            SpeechRecognizer.createOnDeviceSpeechRecognizer(ctx)
        } else {
            usedOnDevice = false
            SpeechRecognizer.createSpeechRecognizer(ctx)
        }.also { it.setRecognitionListener(listener) }

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, onDeviceAvailable)
        }
        recognizer?.startListening(intent)
        return true
    }

    /** Path A intent result hand-off (the Activity calls this). */
    fun onIntentResult(text: String?) = settle(text)

    private fun settle(text: String?) {
        transcript = text?.trim().orEmpty()
        if (transcript.isEmpty()) {
            phase = Phase.ERROR
            diagnostics = "heard nothing"
            return
        }
        diagnostics = "engine: ${if (usedOnDevice) "ON-DEVICE" else "network/system"}"
        sendToPAL(transcript)
    }

    private fun sendToPAL(text: String) {
        val base = ConfigStore.base(ctx)
        val token = ConfigStore.token(ctx)
        if (base.isNullOrEmpty() || token.isNullOrEmpty()) {
            phase = Phase.ERROR
            diagnostics = "not paired"
            return
        }
        phase = Phase.SENDING
        reply = ""
        viewModelScope.launch {
            try {
                val answer = PALClient.command(base, token, text)
                reply = answer.ifEmpty { "(done — no reply text)" }
                phase = Phase.DONE
                vibrate(120)
            } catch (e: Exception) {
                phase = Phase.ERROR
                diagnostics = "PAL: ${e.message}"
                vibrate(60); vibrate(60)
            }
        }
    }

    private fun vibrate(ms: Long) {
        val v = ctx.getSystemService(Vibrator::class.java) ?: return
        v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    private val listener = object : RecognitionListener {
        override fun onPartialResults(p: Bundle) {
            p.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()?.let { transcript = it }
        }
        override fun onResults(p: Bundle) {
            settle(p.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull())
            recognizer?.destroy(); recognizer = null
        }
        override fun onError(error: Int) {
            phase = Phase.ERROR
            diagnostics = "recognizer error $error"
            recognizer?.destroy(); recognizer = null
        }
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    override fun onCleared() {
        recognizer?.destroy()
        recognizer = null
    }
}
