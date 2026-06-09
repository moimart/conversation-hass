package sh.martinez.pal.companion.wear

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Enrollment client — talks to PAL's LAN base (pairing is LAN-only: the
 * gateway deliberately doesn't proxy request/redeem, so a token can only be
 * minted at home). The watch self-enrolls least-privilege (scope="watch").
 */
object PairingClient {

    class Failure(message: String) : Exception(message)

    data class Paired(val token: String, val base: String)

    /** Ask PAL to show a pairing code on the kiosk display. */
    suspend fun requestCode(lanBase: String) = withContext(Dispatchers.IO) {
        post("${lanBase.trimEnd('/')}/api/pair/request", "{}")
    }

    /** Redeem the kiosk code for a watch-scope token. Returns the token + the
     * runtime base to use afterwards (the gateway URL PAL hands back, falling
     * back to the LAN base if none is configured). */
    suspend fun redeem(lanBase: String, code: String): Paired =
        withContext(Dispatchers.IO) {
            val body = JSONObject()
                .put("code", code)
                .put("device_name", "Pixel Watch 3")
                .put("scope", "watch")
                .toString()
            val json = post("${lanBase.trimEnd('/')}/api/pair/redeem", body)
            val token = json.optString("token")
            if (token.isEmpty()) throw Failure(json.optString("error", "pairing failed"))
            val gw = json.optString("gateway_url").ifEmpty { lanBase.trimEnd('/') }
            Paired(token, gw)
        }

    private fun post(url: String, body: String): JSONObject {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = 8_000
            readTimeout = 15_000
            setRequestProperty("Content-Type", "application/json")
        }
        try {
            conn.outputStream.use { it.write(body.toByteArray()) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: "{}"
            if (code !in 200..299) {
                val err = runCatching { JSONObject(text).optString("error") }.getOrNull()
                throw Failure(err?.ifEmpty { "HTTP $code" } ?: "HTTP $code")
            }
            return JSONObject(text)
        } finally {
            conn.disconnect()
        }
    }
}
