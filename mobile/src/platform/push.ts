// Native push registration. Best-effort: no-op on web / when the plugin or
// permission is unavailable, so the same code runs in a dev browser. Registers
// the device's APNs/FCM token with the server (/api/pair/push-register) so PAL
// can notify the device while its app is closed. Android creates the
// notification channels here (sound/importance is per-channel on Android O+).

import { Capacitor } from "@capacitor/core";
import { PushNotifications } from "@capacitor/push-notifications";

let _wired = false;

/** Request permission, register for push, and POST the token to the server.
 *  `serverBaseUrl` is the active (home or gateway) base; `bearer` is the paired
 *  device token. Safe to call on every launch — registration is idempotent and
 *  token refreshes re-fire the listener (re-POSTing the new token). */
export async function registerPush(serverBaseUrl: string, bearer: string): Promise<void> {
  if (Capacitor.getPlatform() === "web") return;   // no native push in a browser
  if (!bearer) return;                             // not paired yet
  try {
    const perm = await PushNotifications.requestPermissions();   // Android 13+ prompt
    if (perm.receive !== "granted") {
      console.log("[push] permission not granted:", perm.receive);
      return;
    }
    if (!_wired) {
      _wired = true;
      await createChannels();
      await PushNotifications.addListener("registration", (t) => {
        void sendToken(serverBaseUrl, bearer, t.value);
      });
      await PushNotifications.addListener("registrationError", (e) => {
        console.warn("[push] registration error", e);
      });
      await PushNotifications.addListener("pushNotificationActionPerformed", () => {
        // Tapping a notification opens the app to the conversation log, if the
        // reused display has mounted its controller by then.
        try {
          (window as unknown as { HALConversationLog?: { open?: () => void } })
            .HALConversationLog?.open?.();
        } catch { /* display not ready — app still opens */ }
      });
    }
    await PushNotifications.register();   // → fires "registration" with the token
  } catch (e) {
    console.warn("[push] register failed", e);
  }
}

async function createChannels(): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;   // iOS has no channels
  try {
    await PushNotifications.createChannel({
      id: "announcements", name: "Announcements",
      description: "Spoken messages and announcements from PAL",
      importance: 4, visibility: 1,
    });
    await PushNotifications.createChannel({
      id: "timers", name: "Timers",
      description: "Finished timers",
      importance: 5, visibility: 1, vibration: true,
    });
  } catch (e) {
    console.warn("[push] channel create failed", e);
  }
}

async function sendToken(base: string, bearer: string, pushToken: string): Promise<void> {
  try {
    const res = await fetch(`${base.replace(/\/+$/, "")}/api/pair/push-register`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${bearer}` },
      body: JSON.stringify({ platform: Capacitor.getPlatform(), push_token: pushToken }),
    });
    console.log("[push] register ->", res.status);
  } catch (e) {
    console.warn("[push] token POST failed", e);
  }
}
