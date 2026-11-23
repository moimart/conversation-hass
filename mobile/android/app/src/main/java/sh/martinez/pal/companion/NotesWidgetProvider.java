package sh.martinez.pal.companion;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.text.format.DateFormat;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

/**
 * Home-screen widget: the latest voice notes, newest first, with a refresh
 * button. Three rows by default, five when the widget is tall enough. Tapping
 * a note opens the app on its Notes view.
 *
 * Data: GET /api/notes?limit=5 with the paired token, home base first (short
 * timeout) and the gateway when away, the same order the app uses. The result
 * is cached so the widget draws instantly and still shows something offline.
 * It refreshes every 30 min (updatePeriodMillis), on the button, and whenever
 * the app opens, closes or records a note.
 */
public class NotesWidgetProvider extends AppWidgetProvider {
    static final String ACTION_REFRESH = "sh.martinez.pal.companion.NOTES_WIDGET_REFRESH";
    static final String EXTRA_OPEN = "pal_open";
    private static final int MAX_ROWS = 5;
    private static final int[] ROW = {R.id.note_row_0, R.id.note_row_1, R.id.note_row_2, R.id.note_row_3, R.id.note_row_4};
    private static final int[] TEXT = {R.id.note_text_0, R.id.note_text_1, R.id.note_text_2, R.id.note_text_3, R.id.note_text_4};
    private static final int[] META = {R.id.note_meta_0, R.id.note_meta_1, R.id.note_meta_2, R.id.note_meta_3, R.id.note_meta_4};

    private static volatile boolean sRefreshing;

    /** Refresh every placed widget (from the app). No-op when none are placed. */
    static void refreshAll(Context ctx) {
        Context app = ctx.getApplicationContext();
        if (ids(app).length == 0) return;
        Intent i = new Intent(app, NotesWidgetProvider.class).setAction(ACTION_REFRESH);
        app.sendBroadcast(i);
    }

    private static int[] ids(Context c) {
        return AppWidgetManager.getInstance(c).getAppWidgetIds(new ComponentName(c, NotesWidgetProvider.class));
    }

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] appWidgetIds) {
        renderAll(ctx);
        fetchAsync(ctx);
    }

    @Override
    public void onAppWidgetOptionsChanged(Context ctx, AppWidgetManager mgr, int id, Bundle opts) {
        render(ctx, mgr, id);
    }

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (ACTION_REFRESH.equals(intent.getAction())) {
            fetchAsync(ctx);
            return;
        }
        super.onReceive(ctx, intent);
    }

    // --- fetch -----------------------------------------------------------------------------

    private void fetchAsync(Context ctx) {
        if (sRefreshing) return;
        sRefreshing = true;
        renderAll(ctx);   // spinner state
        final PendingResult pending = goAsync();
        final Context app = ctx.getApplicationContext();
        new Thread(() -> {
            try {
                fetch(app);
            } finally {
                sRefreshing = false;
                renderAll(app);
                pending.finish();
            }
        }, "pal-notes-widget").start();
    }

    private static void fetch(Context c) {
        PalWidgetStore s = PalWidgetStore.load(c);
        if (!s.configured()) { PalWidgetStore.saveError(c, "setup"); return; }
        String body = null;
        int status = 0;
        // Home first with a short timeout (fails fast when away), then the gateway.
        String[] bases = {s.home, s.gateway};
        int[] timeouts = {2500, 6000};
        for (int i = 0; i < bases.length && body == null; i++) {
            if (bases[i].isEmpty() || (i == 1 && bases[1].equals(bases[0]))) continue;
            try {
                HttpURLConnection h = (HttpURLConnection) new URL(bases[i] + "/api/notes?limit=" + MAX_ROWS).openConnection();
                h.setConnectTimeout(timeouts[i]);
                h.setReadTimeout(6000);
                h.setUseCaches(false);
                if (!s.token.isEmpty()) h.setRequestProperty("Authorization", "Bearer " + s.token);
                status = h.getResponseCode();
                if (status == 200) body = read(h.getInputStream());
                h.disconnect();
                if (status == 401 || status == 403) break;   // a reachable server said no: don't retry elsewhere
            } catch (Exception e) {
                status = 0;
            }
        }
        if (body == null) {
            PalWidgetStore.saveError(c, status == 401 || status == 403 ? "auth" : "offline");
            return;
        }
        try {
            JSONArray rows = new JSONObject(body).optJSONArray("rows");
            JSONArray out = new JSONArray();
            // The page is oldest→newest; the widget lists newest first.
            if (rows != null) for (int i = rows.length() - 1; i >= 0; i--) out.put(rows.getJSONObject(i));
            PalWidgetStore.saveNotes(c, out.toString());
        } catch (Exception e) {
            PalWidgetStore.saveError(c, "offline");
        }
    }

    private static String read(InputStream in) throws Exception {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
        in.close();
        return b.toString("UTF-8");
    }

    // --- render ----------------------------------------------------------------------------

    private static void renderAll(Context c) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(c);
        for (int id : ids(c)) render(c, mgr, id);
    }

    /** Rows that fit: 5 when the widget is about 4 cells tall (~440 dp+), else 3. */
    private static int rowsFor(AppWidgetManager mgr, int id) {
        Bundle o = mgr.getAppWidgetOptions(id);
        int h = o == null ? 0 : o.getInt(AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT, 0);
        return h >= 430 ? 5 : 3;
    }

    private static void render(Context c, AppWidgetManager mgr, int id) {
        RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget_notes);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;

        Intent refresh = new Intent(c, NotesWidgetProvider.class).setAction(ACTION_REFRESH);
        v.setOnClickPendingIntent(R.id.notes_refresh, PendingIntent.getBroadcast(c, 0, refresh, flags));
        v.setViewVisibility(R.id.notes_refresh, sRefreshing ? View.GONE : View.VISIBLE);
        v.setViewVisibility(R.id.notes_progress, sRefreshing ? View.VISIBLE : View.GONE);

        Intent open = new Intent(c, MainActivity.class)
            .putExtra(EXTRA_OPEN, "notes")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(c, 1, open, flags);
        v.setOnClickPendingIntent(R.id.notes_header, openPi);
        v.setOnClickPendingIntent(R.id.notes_empty, openPi);

        JSONArray notes = null;
        try {
            String raw = PalWidgetStore.cachedNotes(c);
            if (!raw.isEmpty()) notes = new JSONArray(raw);
        } catch (Exception ignored) { }
        String err = PalWidgetStore.lastError(c);
        int max = rowsFor(mgr, id);
        int shown = 0;
        for (int i = 0; i < MAX_ROWS; i++) {
            JSONObject n = (notes != null && i < notes.length() && i < max) ? notes.optJSONObject(i) : null;
            if (n == null) { v.setViewVisibility(ROW[i], View.GONE); continue; }
            String text = n.optString("text", "").trim();
            v.setTextViewText(TEXT[i], text.isEmpty() ? "(no words)" : text);
            v.setTextViewText(META[i], meta(c, n));
            v.setViewVisibility(ROW[i], View.VISIBLE);
            v.setOnClickPendingIntent(ROW[i], openPi);
            shown++;
        }

        String status = statusLine(c, err, notes != null);
        v.setTextViewText(R.id.notes_status, status);
        v.setViewVisibility(R.id.notes_status, status.isEmpty() ? View.GONE : View.VISIBLE);
        if (shown == 0) {
            v.setTextViewText(R.id.notes_empty, emptyText(err, notes != null));
            v.setViewVisibility(R.id.notes_empty, View.VISIBLE);
        } else {
            v.setViewVisibility(R.id.notes_empty, View.GONE);
        }
        mgr.updateAppWidget(id, v);
    }

    private static String emptyText(String err, boolean haveData) {
        if ("setup".equals(err)) return "Open PAL once to connect this widget";
        if ("auth".equals(err)) return "This phone isn't paired with PAL any more";
        if (!haveData && "offline".equals(err)) return "Can't reach PAL right now";
        if (!haveData) return "Loading…";
        return "No voice notes yet.\nRecord one with the top button on the PAL Round or the note button in the app.";
    }

    private static String statusLine(Context c, String err, boolean haveData) {
        if (!haveData) return "";
        if ("offline".equals(err)) return "Offline · " + ago(PalWidgetStore.cachedAt(c));
        if ("auth".equals(err)) return "Not paired";
        return "";
    }

    private static String ago(long at) {
        if (at <= 0) return "";
        long m = (System.currentTimeMillis() - at) / 60000;
        if (m < 1) return "just now";
        if (m < 60) return m + " min ago";
        if (m < 24 * 60) return (m / 60) + " h ago";
        return (m / (24 * 60)) + " d ago";
    }

    private static String meta(Context c, JSONObject n) {
        StringBuilder sb = new StringBuilder(when(c, n.optString("ts", "")));
        double d = n.optDouble("duration_s", 0);
        if (d >= 1) {
            int s = (int) Math.round(d);
            if (sb.length() > 0) sb.append(" · ");
            sb.append(s / 60).append(':').append(String.format(Locale.US, "%02d", s % 60));
        }
        return sb.toString();
    }

    /** "Just now", "12 min ago", "14:05", "Yesterday 14:05", "Mon 14:05", "12 Nov". */
    static String when(Context c, String iso) {
        Date d = parseIso(iso);
        if (d == null) return "";
        long diff = System.currentTimeMillis() - d.getTime();
        if (diff < 60_000) return "Just now";
        if (diff < 60 * 60_000) return (diff / 60_000) + " min ago";
        String time = DateFormat.getTimeFormat(c).format(d);
        Calendar now = Calendar.getInstance(), then = Calendar.getInstance();
        then.setTime(d);
        int days = dayIndex(now) - dayIndex(then);
        if (days == 0) return time;
        if (days == 1) return "Yesterday " + time;
        if (days < 7) return new SimpleDateFormat("EEE", Locale.getDefault()).format(d) + " " + time;
        String skel = now.get(Calendar.YEAR) == then.get(Calendar.YEAR) ? "dMMM" : "dMMMyyyy";
        return new SimpleDateFormat(DateFormat.getBestDateTimePattern(Locale.getDefault(), skel), Locale.getDefault()).format(d);
    }

    private static int dayIndex(Calendar c) {
        return c.get(Calendar.YEAR) * 400 + c.get(Calendar.DAY_OF_YEAR);
    }

    /** The server's ISO-8601 timestamp (with offset, maybe fractional seconds). */
    static Date parseIso(String iso) {
        if (iso == null || iso.length() < 19) return null;
        try {
            String s = iso.replace('T', ' ');
            String base = s.substring(0, 19);
            String rest = s.substring(19);
            int msec = 0;
            if (rest.startsWith(".")) {
                int j = 1;
                while (j < rest.length() && Character.isDigit(rest.charAt(j))) j++;
                String frac = (rest.substring(1, j) + "000").substring(0, 3);
                msec = Integer.parseInt(frac);
                rest = rest.substring(j);
            }
            TimeZone tz;
            if (rest.isEmpty() || rest.equals("Z")) tz = TimeZone.getTimeZone("UTC");
            else tz = TimeZone.getTimeZone("GMT" + rest);
            SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US);
            f.setTimeZone(tz);
            Date d = f.parse(base);
            return d == null ? null : new Date(d.getTime() + msec);
        } catch (Exception e) {
            return null;
        }
    }
}
