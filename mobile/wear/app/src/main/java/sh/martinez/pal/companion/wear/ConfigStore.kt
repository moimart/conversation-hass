package sh.martinez.pal.companion.wear

import android.content.Context

/**
 * Persisted pairing config: the watch-scope token + the runtime base it talks
 * to (the gateway). App-sandboxed SharedPreferences — same posture as the
 * Capacitor phone app's @capacitor/preferences; Keystore hardening is a noted
 * follow-up. Replaces the old gitignored DevConfig.kt.
 */
object ConfigStore {
    private const val PREFS = "pal_wear"

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun isPaired(ctx: Context) = !token(ctx).isNullOrEmpty()

    fun token(ctx: Context): String? = prefs(ctx).getString("token", null)

    /** Runtime base PAL is reached at (the gateway HTTPS URL). */
    fun base(ctx: Context): String? = prefs(ctx).getString("base", null)

    fun save(ctx: Context, token: String, base: String) {
        prefs(ctx).edit().putString("token", token).putString("base", base).apply()
    }

    fun clear(ctx: Context) {
        prefs(ctx).edit().clear().apply()
    }
}
