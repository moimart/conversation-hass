import UserNotifications

// Notification Service Extension: when an APNs push is sent with
// "mutable-content": 1 and carries an "image_url" in its payload, download that
// image and attach it so the notification shows an inline thumbnail (Tier-B).
//
// The URL is a short-lived, HMAC-signed link to PAL's own gateway
// (/api/push/image/{id}.jpg) — the image BYTES come from there, never from
// Apple. No pairing token or App Group is needed: the signature in the URL is
// what authorizes the fetch, validated server-side.
class NotificationService: UNNotificationServiceExtension {

    var contentHandler: ((UNNotificationContent) -> Void)?
    var bestAttempt: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        self.contentHandler = contentHandler
        let mutable = request.content.mutableCopy() as? UNMutableNotificationContent
        self.bestAttempt = mutable

        guard let content = mutable,
              let urlString = content.userInfo["image_url"] as? String,
              let url = URL(string: urlString) else {
            contentHandler(request.content)
            return
        }

        let task = URLSession.shared.downloadTask(with: url) { location, _, _ in
            defer { contentHandler(content) }
            guard let location = location else { return }
            // The signed URL ends in .jpg; give the temp file that extension so
            // UNNotificationAttachment can infer the image type.
            let tmp = FileManager.default.temporaryDirectory
                .appendingPathComponent(UUID().uuidString + ".jpg")
            try? FileManager.default.moveItem(at: location, to: tmp)
            if let attachment = try? UNNotificationAttachment(identifier: "image", url: tmp) {
                content.attachments = [attachment]
            }
        }
        task.resume()
    }

    override func serviceExtensionTimeWillExpire() {
        // Called just before the extension is killed; deliver whatever we have.
        if let contentHandler = contentHandler, let bestAttempt = bestAttempt {
            contentHandler(bestAttempt)
        }
    }
}
