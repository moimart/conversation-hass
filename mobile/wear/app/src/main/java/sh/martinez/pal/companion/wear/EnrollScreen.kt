package sh.martinez.pal.companion.wear

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.ChipDefaults
import androidx.wear.compose.material.Text
import kotlinx.coroutines.launch

/**
 * One-time enrollment: enter PAL's LAN URL, get a code on the kiosk, type it,
 * redeem it scoped=watch, persist. Pairing is LAN-only by design.
 */
@Composable
fun EnrollScreen(defaultLan: String, onPaired: () -> Unit) {
    var lan by remember { mutableStateOf(defaultLan) }
    var code by remember { mutableStateOf("") }
    var status by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current

    // Best-effort LAN autodiscovery: while the field still holds the untouched
    // default, replace it with the ai-server found on the network.
    LaunchedEffect(Unit) {
        WearDiscovery.discover(ctx) { url ->
            if (lan == defaultLan) { lan = url; status = "found PAL on your network" }
        }
    }

    Column(
        Modifier.fillMaxSize().background(Color.Black)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 12.dp, vertical = 20.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Text("Pair PAL", color = Color.White, fontSize = 16.sp)
        Text("PAL server (LAN)", color = Color.Gray, fontSize = 10.sp)
        Field(lan, { lan = it }, KeyboardType.Uri)

        Chip(
            label = { Text("Show code on kiosk", fontSize = 12.sp) },
            onClick = {
                if (busy) return@Chip
                busy = true; status = "asking PAL…"
                scope.launch {
                    status = try {
                        PairingClient.requestCode(lan); "code shown on kiosk"
                    } catch (e: Exception) { "err: ${e.message}" }
                    busy = false
                }
            },
            colors = ChipDefaults.secondaryChipColors(),
            modifier = Modifier.fillMaxWidth(),
        )

        Text("Code from kiosk", color = Color.Gray, fontSize = 10.sp)
        Field(code, { code = it.filter { c -> c.isDigit() }.take(6) }, KeyboardType.Number)

        Chip(
            label = { Text(if (busy) "…" else "Pair", fontSize = 13.sp) },
            onClick = {
                if (busy || code.length < 4) { status = "enter the code"; return@Chip }
                busy = true; status = "pairing…"
                scope.launch {
                    try {
                        val p = PairingClient.redeem(lan, code)
                        // store the runtime base (gateway) + token
                        ConfigStore.save(ctx, p.token, p.base)
                        onPaired()
                    } catch (e: Exception) {
                        status = "failed: ${e.message}"; busy = false
                    }
                }
            },
            colors = ChipDefaults.primaryChipColors(),
            modifier = Modifier.fillMaxWidth(),
        )
        if (status.isNotEmpty()) {
            Text(status, color = Color.Cyan, fontSize = 10.sp, textAlign = TextAlign.Center)
        }
    }
}

@Composable
private fun Field(value: String, onChange: (String) -> Unit, type: KeyboardType) {
    BasicTextField(
        value = value,
        onValueChange = onChange,
        singleLine = true,
        textStyle = TextStyle(color = Color.White, fontSize = 13.sp, textAlign = TextAlign.Center),
        keyboardOptions = KeyboardOptions(keyboardType = type),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, Color.DarkGray, RoundedCornerShape(8.dp))
            .padding(8.dp),
    )
}
