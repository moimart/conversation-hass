package sh.martinez.pal.companion.wear

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Registers this watch's FCM token with PAL so finished timers / announcements
 * buzz the wrist while the app is closed. The watch is "another android device"
 * to PAL's FcmSender — no server change. Channels match the server's
 * build_fcm_message: "timers" (high) + "announcements".
 */
object PushRegister {

    /** Notification channels must exist for FCM notification messages to show. */
    fun ensureChannels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel("timers", "Timers", NotificationManager.IMPORTANCE_HIGH)
                .apply { enableVibration(true) })
        nm.createNotificationChannel(
            NotificationChannel("announcements", "Announcements",
                NotificationManager.IMPORTANCE_DEFAULT))
    }

    /** Fetch the FCM token and register it with PAL (bearer = the watch token). */
    fun register(ctx: Context) {
        val base = ConfigStore.base(ctx) ?: return
        val bearer = ConfigStore.token(ctx) ?: return
        FirebaseMessaging.getInstance().token.addOnSuccessListener { fcm ->
            CoroutineScope(Dispatchers.IO).launch { post(base, bearer, fcm) }
        }
    }

    private fun post(base: String, bearer: String, fcmToken: String) {
        val conn = (URL("${base.trimEnd('/')}/api/pair/push-register")
            .openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = 10_000
            readTimeout = 15_000
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Authorization", "Bearer $bearer")
        }
        try {
            val body = JSONObject()
                .put("platform", "android")
                .put("push_token", fcmToken)
                .toString()
            conn.outputStream.use { it.write(body.toByteArray()) }
            conn.responseCode   // fire-and-forget; nothing to do with the result
        } catch (_: Exception) {
            // best-effort; retried on next launch / token rotation
        } finally {
            conn.disconnect()
        }
    }
}
