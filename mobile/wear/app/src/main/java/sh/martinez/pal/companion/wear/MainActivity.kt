package sh.martinez.pal.companion.wear

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.wear.compose.material.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Wear PTT spike screen: tap the orb -> dictation. Prefers in-app recognition
 * (Path B); falls back to the system speech intent (Path A) when no in-app
 * backend exists. The diagnostics line is the spike verdict.
 */
class MainActivity : ComponentActivity() {

    private val speech: SpeechManager by viewModels()

    private val speechIntent = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val text = if (result.resultCode == Activity.RESULT_OK) {
            result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
        } else null
        speech.onIntentResult(text)
    }

    private val micPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) beginListening() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { OrbScreen(speech, onTap = ::onOrbTap) }
    }

    private fun onOrbTap() {
        if (speech.phase == SpeechManager.Phase.LISTENING) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED) {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        } else {
            beginListening()
        }
    }

    private fun beginListening() {
        // Try Path B (in-app, orb stays on screen); fall back to Path A intent.
        if (!speech.startPathB()) {
            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                putExtra(RecognizerIntent.EXTRA_PROMPT, "PAL command")
            }
            speechIntent.launch(intent)
        }
    }
}

@Composable
private fun OrbScreen(speech: SpeechManager, onTap: () -> Unit) {
    Box(
        Modifier.fillMaxSize().background(Color.Black),
        contentAlignment = Alignment.Center
    ) {
        Column(
            Modifier.verticalScroll(rememberScrollState()).padding(8.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            val colors = when (speech.phase) {
                SpeechManager.Phase.LISTENING -> listOf(Color.Cyan, Color(0x3300008B))
                SpeechManager.Phase.SENDING -> listOf(Color(0xFF9C27B0), Color(0x334B0082))
                SpeechManager.Phase.DONE -> listOf(Color.Green, Color(0x33008080))
                SpeechManager.Phase.ERROR -> listOf(Color(0xFFFF9800), Color(0x33FF0000))
                else -> listOf(Color(0xFF26A69A), Color(0x33000080))
            }
            Box(
                Modifier
                    .size(80.dp)
                    .clip(CircleShape)
                    .background(Brush.radialGradient(colors))
                    .clickable { onTap() }
            )
            Text(
                statusLine(speech.phase),
                color = Color.Gray, fontSize = 11.sp,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 6.dp)
            )
            if (speech.transcript.isNotEmpty()) {
                Text("“${speech.transcript}”", color = Color.Gray, fontSize = 11.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.padding(top = 4.dp))
            }
            if (speech.reply.isNotEmpty()) {
                Text(speech.reply, color = Color.White, fontSize = 14.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.padding(top = 4.dp))
            }
            // Error detail only — the spike verdict/engine line is gone.
            if (speech.phase == SpeechManager.Phase.ERROR && speech.diagnostics.isNotEmpty()) {
                Text(speech.diagnostics, color = Color(0xFFFF9800), fontSize = 11.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.padding(top = 6.dp))
            }
        }
    }
}

private fun statusLine(phase: SpeechManager.Phase) = when (phase) {
    SpeechManager.Phase.IDLE -> "tap to speak"
    SpeechManager.Phase.LISTENING -> "listening…"
    SpeechManager.Phase.SENDING -> "asking PAL…"
    SpeechManager.Phase.DONE -> "tap to speak again"
    SpeechManager.Phase.ERROR -> "error — tap to retry"
}
