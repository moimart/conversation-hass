package sh.martinez.pal.companion.wear

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal PAL API client for the watch: one call, `command`, which POSTs the
 * dictated text with wait_reply=true so PAL's reply comes back in the HTTP
 * response (the watch scope has no /ws/ui channel for async replies). Talks to
 * the gateway base over HTTPS — the same path as away-from-home use.
 */
object PALClient {

    class Failure(message: String) : Exception(message)

    /** Send a command, return PAL's reply text ("" if none). */
    suspend fun command(text: String): String = withContext(Dispatchers.IO) {
        val url = URL("${DevConfig.SERVER_BASE}/api/command")
        val conn = (url.openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = 10_000
            readTimeout = 95_000          // server caps the turn at 90s
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Authorization", "Bearer ${DevConfig.WATCH_TOKEN}")
        }
        try {
            val body = JSONObject()
                .put("text", text)
                .put("wait_reply", true)
                .toString()
            conn.outputStream.use { it.write(body.toByteArray()) }

            val code = conn.responseCode
            if (code != 200) throw Failure("HTTP $code")
            val resp = conn.inputStream.bufferedReader().use { it.readText() }
            JSONObject(resp).optString("reply", "").trim()
        } finally {
            conn.disconnect()
        }
    }
}
