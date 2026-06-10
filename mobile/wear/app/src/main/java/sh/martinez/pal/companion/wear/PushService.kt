package sh.martinez.pal.companion.wear

import android.app.Notification
import android.app.NotificationManager
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * Receives PAL's FCM pushes. When the app is backgrounded/closed (the usual
 * case for a timer firing) the system auto-displays the notification on the
 * channel — this service only needs to re-register a rotated token and to
 * surface a notification if a push arrives while the app is foreground.
 */
class PushService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        PushRegister.register(applicationContext)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val n = message.notification ?: return
        val channel = n.channelId?.takeIf { it.isNotEmpty() } ?: "announcements"
        PushRegister.ensureChannels(applicationContext)
        val notif = Notification.Builder(this, channel)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(n.title ?: "PAL")
            .setContentText(n.body ?: "")
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java)
            ?.notify(message.messageId?.hashCode() ?: 0, notif)
    }
}
