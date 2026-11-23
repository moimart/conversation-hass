package sh.martinez.pal.companion;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * What the home-screen widgets need to reach PAL without the WebView: the home
 * and gateway bases, the device token, and the last notes they showed. The app
 * hands these over on every launch (PalWidgetPlugin.configure).
 *
 * The token is a house key, so it is never stored in plain text: it is
 * AES-GCM encrypted with a key that lives in the Android Keystore and can't
 * leave it. The bases and cached notes are not secret.
 */
final class PalWidgetStore {
    private static final String PREFS = "pal_widget";
    private static final String KEY_ALIAS = "pal_widget_token";

    final String home, gateway, token;

    private PalWidgetStore(String home, String gateway, String token) {
        this.home = home; this.gateway = gateway; this.token = token;
    }

    boolean configured() { return !home.isEmpty() || !gateway.isEmpty(); }

    private static SharedPreferences prefs(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static PalWidgetStore load(Context c) {
        SharedPreferences p = prefs(c);
        String tok = "";
        String enc = p.getString("token", "");
        if (!enc.isEmpty()) {
            try { tok = decrypt(enc); } catch (Exception e) { tok = ""; }
        }
        return new PalWidgetStore(p.getString("home", ""), p.getString("gateway", ""), tok);
    }

    static void save(Context c, String home, String gateway, String token) {
        String enc = "";
        if (token != null && !token.isEmpty()) {
            try { enc = encrypt(token); } catch (Exception e) { enc = ""; }
        }
        prefs(c).edit()
            .putString("home", trim(home))
            .putString("gateway", trim(gateway))
            .putString("token", enc)
            .apply();
    }

    static void clear(Context c) { prefs(c).edit().clear().apply(); }

    // --- cached notes (newest first, JSON array) ---------------------------------------

    static String cachedNotes(Context c) { return prefs(c).getString("notes", ""); }
    static long cachedAt(Context c) { return prefs(c).getLong("notes_at", 0L); }
    static String lastError(Context c) { return prefs(c).getString("error", ""); }

    static void saveNotes(Context c, String json) {
        prefs(c).edit().putString("notes", json).putLong("notes_at", System.currentTimeMillis())
            .putString("error", "").apply();
    }

    static void saveError(Context c, String err) {
        prefs(c).edit().putString("error", err == null ? "" : err).apply();
    }

    // --- helpers --------------------------------------------------------------------------

    private static String trim(String s) {
        if (s == null) return "";
        s = s.trim();
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    private static SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (ks.containsAlias(KEY_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) ks.getEntry(KEY_ALIAS, null)).getSecretKey();
        }
        KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        kg.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build());
        return kg.generateKey();
    }

    private static String encrypt(String plain) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, key());
        byte[] iv = c.getIV();
        byte[] ct = c.doFinal(plain.getBytes(StandardCharsets.UTF_8));
        return Base64.encodeToString(iv, Base64.NO_WRAP) + ":" + Base64.encodeToString(ct, Base64.NO_WRAP);
    }

    private static String decrypt(String enc) throws Exception {
        int i = enc.indexOf(':');
        byte[] iv = Base64.decode(enc.substring(0, i), Base64.NO_WRAP);
        byte[] ct = Base64.decode(enc.substring(i + 1), Base64.NO_WRAP);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
        return new String(c.doFinal(ct), StandardCharsets.UTF_8);
    }
}
