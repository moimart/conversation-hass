// Home Assistant pages. The server turns a dashboard into pages of widgets
// (/api/device/pages → [{title, widgets:[{e, k, i, n?}]}]); each page is a
// tile right of the home screen. Live state arrives on the socket (ha_states /
// ha_state for the retained entities); actions go to /api/ha/service.
//
// Kinds: light, toggle, cover, lock, action (scene/script/button), sensor →
// round tiles, 4 to a page (tap = toggle/run, long-press a light/cover = a
// brightness/position dial). climate, media, weather → a whole page.
#include <math.h>
#include <string.h>
#include "app.h"
#include "esp_log.h"

static const char *TAG = "ha";

#define MAX_W 64

typedef struct {
    char e[64];
    char kind[12];
    uint32_t icon;
    char name[44];
    lv_obj_t *tile, *ic, *lname, *lstate;
    lv_obj_t *arc, *big, *sub, *btn_play;   // big widgets
} widget_t;

static widget_t s_w[MAX_W];
static int s_n;
static int s_pages;
static cJSON *s_states;      // entity_id -> state object
static lv_obj_t *s_dial;     // brightness/position modal

static widget_t *find(const char *e)
{
    for (int i = 0; i < s_n; i++)
        if (!strcmp(s_w[i].e, e)) return &s_w[i];
    return NULL;
}

static cJSON *state_of(const char *e) { return s_states ? cJSON_GetObjectItem(s_states, e) : NULL; }

static const char *st_str(cJSON *s) { return s ? cJSON_GetStringValue(cJSON_GetObjectItem(s, "state")) : NULL; }

static cJSON *attr(cJSON *s, const char *k) { return s ? cJSON_GetObjectItem(cJSON_GetObjectItem(s, "attributes"), k) : NULL; }

static bool is_on(const widget_t *w, cJSON *s)
{
    const char *v = st_str(s);
    if (!v) return false;
    if (!strcmp(w->kind, "lock")) return !strcmp(v, "unlocked") || !strcmp(v, "open");
    if (!strcmp(w->kind, "cover")) return !strcmp(v, "open") || !strcmp(v, "opening");
    return !strcmp(v, "on") || !strcmp(v, "playing") || !strcmp(v, "home") || !strcmp(v, "heat") ||
           !strcmp(v, "cool") || !strcmp(v, "auto") || !strcmp(v, "cleaning");
}

static void fmt_number(const char *v, char *out, size_t n)
{
    char *end;
    double d = strtod(v, &end);
    if (end != v && *end == 0) {
        if (fabs(d - round(d)) < 0.05) snprintf(out, n, "%.0f", d);
        else snprintf(out, n, "%.1f", d);
    } else {
        strlcpy(out, v, n);
        if (out[0] >= 'a' && out[0] <= 'z') out[0] -= 32;  // "on" → "On"
        for (char *p = out; *p; p++) if (*p == '_') *p = ' ';
    }
}

static void state_text(const widget_t *w, cJSON *s, char *out, size_t n)
{
    const char *v = st_str(s);
    if (!v) { strlcpy(out, "—", n); return; }
    if (!strcmp(v, "unavailable") || !strcmp(v, "unknown")) { strlcpy(out, "Unavailable", n); return; }
    if (!strcmp(w->kind, "light") && !strcmp(v, "on")) {
        cJSON *b = attr(s, "brightness");
        if (cJSON_IsNumber(b)) { snprintf(out, n, "On · %d%%", (int)lround(b->valuedouble * 100 / 255)); return; }
    }
    if (!strcmp(w->kind, "action")) { strlcpy(out, "Tap to run", n); return; }
    char num[32];
    fmt_number(v, num, sizeof(num));
    const char *unit = cJSON_GetStringValue(attr(s, "unit_of_measurement"));
    if (unit) snprintf(out, n, "%s %s", num, unit);
    else strlcpy(out, num, n);
}

static const char *display_name(const widget_t *w, cJSON *s)
{
    if (w->name[0]) return w->name;
    const char *f = cJSON_GetStringValue(attr(s, "friendly_name"));
    return f ? f : w->e;
}

// --- weather icons -----------------------------------------------------------------

static uint32_t weather_icon(const char *c)
{
    if (!c) return ICON_WEATHER_PARTLY_CLOUDY;
    static const struct { const char *c; uint32_t i; } map[] = {
        {"sunny", ICON_WEATHER_SUNNY}, {"clear-night", ICON_WEATHER_NIGHT}, {"partlycloudy", ICON_WEATHER_PARTLY_CLOUDY},
        {"cloudy", ICON_WEATHER_CLOUDY}, {"rainy", ICON_WEATHER_RAINY}, {"pouring", ICON_WEATHER_POURING},
        {"snowy", ICON_WEATHER_SNOWY}, {"snowy-rainy", ICON_WEATHER_SNOWY_RAINY}, {"lightning", ICON_WEATHER_LIGHTNING},
        {"lightning-rainy", ICON_WEATHER_LIGHTNING_RAINY}, {"fog", ICON_WEATHER_FOG}, {"hail", ICON_WEATHER_HAIL},
        {"windy", ICON_WEATHER_WINDY}, {"windy-variant", ICON_WEATHER_WINDY}, {"exceptional", ICON_ALERT_CIRCLE_OUTLINE},
    };
    for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++)
        if (!strcmp(map[i].c, c)) return map[i].i;
    return ICON_WEATHER_PARTLY_CLOUDY;
}

uint32_t ha_weather_icon(const char *condition) { return weather_icon(condition); }

// --- refresh -------------------------------------------------------------------------

static void refresh(widget_t *w)
{
    cJSON *s = state_of(w->e);
    char buf[64], ic[6];
    bool on = is_on(w, s);
    if (w->lname) lv_label_set_text(w->lname, display_name(w, s));
    if (!strcmp(w->kind, "climate")) {
        cJSON *cur = attr(s, "current_temperature"), *tgt = attr(s, "temperature");
        cJSON *lo = attr(s, "min_temp"), *hi = attr(s, "max_temp");
        if (cJSON_IsNumber(lo) && cJSON_IsNumber(hi))
            lv_arc_set_range(w->arc, (int)(lo->valuedouble * 2), (int)(hi->valuedouble * 2));
        if (cJSON_IsNumber(tgt)) {
            lv_arc_set_value(w->arc, (int)lround(tgt->valuedouble * 2));
            snprintf(buf, sizeof(buf), "%.1f°", tgt->valuedouble);
            lv_label_set_text(w->big, buf);
        } else {
            lv_label_set_text(w->big, "—");
        }
        const char *mode = st_str(s);
        snprintf(buf, sizeof(buf), "%s%s%.1f° now", mode ? mode : "", mode ? " · " : "",
                 cJSON_IsNumber(cur) ? cur->valuedouble : 0.0);
        lv_label_set_text(w->sub, buf);
        return;
    }
    if (!strcmp(w->kind, "media")) {
        const char *title = cJSON_GetStringValue(attr(s, "media_title"));
        const char *artist = cJSON_GetStringValue(attr(s, "media_artist"));
        lv_label_set_text(w->big, title ? title : (st_str(s) ? st_str(s) : "—"));
        lv_label_set_text(w->sub, artist ? artist : "");
        lv_label_set_text(lv_obj_get_child(w->btn_play, 0),
                          mdi(st_str(s) && !strcmp(st_str(s), "playing") ? ICON_PAUSE : ICON_PLAY, ic));
        cJSON *vol = attr(s, "volume_level");
        if (cJSON_IsNumber(vol)) lv_arc_set_value(w->arc, (int)lround(vol->valuedouble * 100));
        return;
    }
    if (!strcmp(w->kind, "weather")) {
        cJSON *t = attr(s, "temperature");
        const char *unit = cJSON_GetStringValue(attr(s, "temperature_unit"));
        if (cJSON_IsNumber(t)) snprintf(buf, sizeof(buf), "%.0f%s", t->valuedouble, unit ? unit : "°");
        else strlcpy(buf, "—", sizeof(buf));
        lv_label_set_text(w->big, buf);
        lv_label_set_text(w->ic, mdi(weather_icon(st_str(s)), ic));
        cJSON *h = attr(s, "humidity"), *wind = attr(s, "wind_speed");
        char cond[32];
        fmt_number(st_str(s) ? st_str(s) : "", cond, sizeof(cond));
        if (!strcmp(cond, "Partlycloudy")) strcpy(cond, "Partly cloudy");
        snprintf(buf, sizeof(buf), "%s · %d%% · %.0f %s", cond, cJSON_IsNumber(h) ? h->valueint : 0,
                 cJSON_IsNumber(wind) ? wind->valuedouble : 0.0,
                 cJSON_GetStringValue(attr(s, "wind_speed_unit")) ? cJSON_GetStringValue(attr(s, "wind_speed_unit")) : "");
        lv_label_set_text(w->sub, buf);
        return;
    }
    // tiles
    state_text(w, s, buf, sizeof(buf));
    lv_label_set_text(w->lstate, buf);
    bool interactive = strcmp(w->kind, "sensor") != 0;
    lv_obj_set_style_bg_color(w->tile, on ? g_theme.accent : lv_color_hex(0x1c1c22), 0);
    lv_obj_set_style_bg_opa(w->tile, on ? LV_OPA_80 : LV_OPA_COVER, 0);
    lv_color_t fg = on ? lv_color_hex(0x0a0a0c) : g_theme.text;
    lv_obj_set_style_text_color(w->lname, fg, 0);
    lv_obj_set_style_text_color(w->lstate, on ? lv_color_hex(0x202024) : g_theme.text_dim, 0);
    lv_obj_set_style_text_color(w->ic, on ? lv_color_hex(0x0a0a0c) : (interactive ? g_theme.accent : g_theme.text_dim), 0);
}

static void refresh_all(void)
{
    for (int i = 0; i < s_n; i++) refresh(&s_w[i]);
}

// --- actions -----------------------------------------------------------------------------

static void toggle(widget_t *w)
{
    const char *domain_end = strchr(w->e, '.');
    char domain[24];
    size_t dl = domain_end ? (size_t)(domain_end - w->e) : 0;
    if (dl >= sizeof(domain)) dl = sizeof(domain) - 1;
    memcpy(domain, w->e, dl);
    domain[dl] = 0;
    cJSON *s = state_of(w->e);
    if (!strcmp(w->kind, "action")) {
        const char *svc = !strcmp(domain, "button") || !strcmp(domain, "input_button") ? "press" : "turn_on";
        pal_call_service(domain, svc, w->e, NULL);
        ui_toast("Done");
        return;
    }
    if (!strcmp(w->kind, "lock")) {
        pal_call_service("lock", is_on(w, s) ? "lock" : "unlock", w->e, NULL);
        return;
    }
    if (!strcmp(w->kind, "cover")) {
        pal_call_service("cover", "toggle", w->e, NULL);
        return;
    }
    if (!strcmp(w->kind, "sensor")) return;
    pal_call_service("homeassistant", "toggle", w->e, NULL);
    // optimistic flip until HA reports back
    if (s) {
        cJSON *st = cJSON_GetObjectItem(s, "state");
        if (cJSON_IsString(st)) cJSON_SetValuestring(st, is_on(w, s) ? "off" : "on");
        refresh(w);
    }
}

static void tile_cb(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    ui_activity();
    toggle(w);
}

static void dial_close(lv_event_t *e)
{
    if (s_dial) {
        lv_obj_delete_async(s_dial);
        s_dial = NULL;
    }
}

static void dial_release(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    lv_obj_t *arc = lv_event_get_target(e);
    int v = lv_arc_get_value(arc);
    cJSON *d = cJSON_CreateObject();
    if (!strcmp(w->kind, "light")) {
        cJSON_AddNumberToObject(d, "brightness_pct", v);
        pal_call_service("light", "turn_on", w->e, d);
    } else {
        cJSON_AddNumberToObject(d, "position", v);
        pal_call_service("cover", "set_cover_position", w->e, d);
    }
}

static void dial_changed(lv_event_t *e)
{
    lv_obj_t *arc = lv_event_get_target(e);
    lv_obj_t *lbl = lv_event_get_user_data(e);
    lv_label_set_text_fmt(lbl, "%d%%", (int)lv_arc_get_value(arc));
    ui_activity();
}

static void open_dial(widget_t *w)
{
    cJSON *s = state_of(w->e);
    int v = 0;
    if (!strcmp(w->kind, "light")) {
        cJSON *b = attr(s, "brightness");
        v = cJSON_IsNumber(b) && is_on(w, s) ? (int)lround(b->valuedouble * 100 / 255) : 0;
    } else {
        cJSON *p = attr(s, "current_position");
        v = cJSON_IsNumber(p) ? p->valueint : 0;
    }
    s_dial = lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(s_dial);
    lv_obj_set_size(s_dial, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(s_dial, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_dial, LV_OPA_COVER, 0);
    lv_obj_t *arc = lv_arc_create(s_dial);
    lv_obj_set_size(arc, 420, 420);
    lv_obj_center(arc);
    lv_arc_set_rotation(arc, 135);
    lv_arc_set_bg_angles(arc, 0, 270);
    lv_arc_set_range(arc, 0, 100);
    lv_arc_set_value(arc, v);
    lv_obj_set_style_arc_width(arc, 26, LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 26, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(arc, g_theme.accent, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(arc, lv_color_hex(0x26262c), LV_PART_MAIN);
    lv_obj_set_style_bg_color(arc, lv_color_white(), LV_PART_KNOB);
    lv_obj_t *pct = lv_label_create(s_dial);
    lv_obj_set_style_text_font(pct, g_fonts.text40, 0);
    lv_obj_set_style_text_color(pct, g_theme.text, 0);
    lv_label_set_text_fmt(pct, "%d%%", v);
    lv_obj_align(pct, LV_ALIGN_CENTER, 0, -10);
    lv_obj_t *nm = lv_label_create(s_dial);
    lv_obj_set_style_text_font(nm, g_fonts.text16, 0);
    lv_obj_set_style_text_color(nm, g_theme.text_dim, 0);
    lv_label_set_text(nm, display_name(w, s));
    lv_obj_align(nm, LV_ALIGN_CENTER, 0, 36);
    lv_obj_t *close = lv_button_create(s_dial);
    lv_obj_set_size(close, 72, 72);
    lv_obj_align(close, LV_ALIGN_BOTTOM_MID, 0, -18);
    lv_obj_set_style_radius(close, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(close, lv_color_hex(0x26262c), 0);
    lv_obj_t *x = lv_label_create(close);
    char ic[6];
    lv_obj_set_style_text_font(x, g_fonts.icon34, 0);
    lv_label_set_text(x, mdi(ICON_CHECK, ic));
    lv_obj_center(x);
    lv_obj_add_event_cb(close, dial_close, LV_EVENT_CLICKED, NULL);
    lv_obj_add_event_cb(arc, dial_changed, LV_EVENT_VALUE_CHANGED, pct);
    lv_obj_add_event_cb(arc, dial_release, LV_EVENT_RELEASED, w);
}

static void tile_long(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    ui_activity();
    if (!strcmp(w->kind, "light") || !strcmp(w->kind, "cover")) {
        lv_obj_add_state(w->tile, LV_STATE_DISABLED);  // swallow the click that follows
        lv_obj_remove_state(w->tile, LV_STATE_DISABLED);
        open_dial(w);
    }
}

static void climate_release(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    cJSON *d = cJSON_CreateObject();
    cJSON_AddNumberToObject(d, "temperature", lv_arc_get_value(w->arc) / 2.0);
    pal_call_service("climate", "set_temperature", w->e, d);
}

static void climate_changed(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    char b[16];
    snprintf(b, sizeof(b), "%.1f°", lv_arc_get_value(w->arc) / 2.0);
    lv_label_set_text(w->big, b);
    ui_activity();
}

static void media_btn(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    const char *svc = lv_obj_get_user_data(lv_event_get_target(e));
    ui_activity();
    pal_call_service("media_player", svc, w->e, NULL);
}

static void media_vol(lv_event_t *e)
{
    widget_t *w = lv_event_get_user_data(e);
    cJSON *d = cJSON_CreateObject();
    cJSON_AddNumberToObject(d, "volume_level", lv_arc_get_value(w->arc) / 100.0);
    pal_call_service("media_player", "volume_set", w->e, d);
}

// --- building ----------------------------------------------------------------------------

static lv_obj_t *label(lv_obj_t *p, const lv_font_t *f, lv_color_t c)
{
    lv_obj_t *l = lv_label_create(p);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, c, 0);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    return l;
}

static void build_tile(lv_obj_t *page, widget_t *w, int cx, int cy, int sz)
{
    char ic[6];
    w->tile = lv_button_create(page);
    lv_obj_set_size(w->tile, sz, sz);
    lv_obj_set_pos(w->tile, cx - sz / 2, cy - sz / 2);
    lv_obj_set_style_radius(w->tile, sz / 4, 0);
    lv_obj_set_style_shadow_width(w->tile, 0, 0);
    lv_obj_set_style_pad_all(w->tile, 8, 0);
    lv_obj_add_flag(w->tile, LV_OBJ_FLAG_EVENT_BUBBLE);  // swipes still reach the tileview
    w->ic = label(w->tile, sz >= 180 ? g_fonts.icon56 : g_fonts.icon34, g_theme.accent);
    lv_label_set_text(w->ic, mdi(w->icon, ic));
    lv_obj_align(w->ic, LV_ALIGN_TOP_MID, 0, sz / 14);
    w->lname = label(w->tile, g_fonts.text16, g_theme.text);
    lv_obj_set_width(w->lname, sz - 16);
    lv_label_set_long_mode(w->lname, LV_LABEL_LONG_DOT);
    lv_obj_set_height(w->lname, 42);
    lv_obj_align(w->lname, LV_ALIGN_CENTER, 0, sz / 10);
    w->lstate = label(w->tile, g_fonts.text16, g_theme.text_dim);
    lv_obj_set_width(w->lstate, sz - 16);
    lv_label_set_long_mode(w->lstate, LV_LABEL_LONG_DOT);
    lv_obj_align(w->lstate, LV_ALIGN_BOTTOM_MID, 0, -sz / 16);
    lv_obj_add_event_cb(w->tile, tile_cb, LV_EVENT_SHORT_CLICKED, w);
    lv_obj_add_event_cb(w->tile, tile_long, LV_EVENT_LONG_PRESSED, w);
}

static lv_obj_t *page_arc(lv_obj_t *page, int lo, int hi)
{
    lv_obj_t *arc = lv_arc_create(page);
    lv_obj_set_size(arc, 430, 430);
    lv_obj_center(arc);
    lv_arc_set_rotation(arc, 135);
    lv_arc_set_bg_angles(arc, 0, 270);
    lv_arc_set_range(arc, lo, hi);
    lv_obj_set_style_arc_width(arc, 18, LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 18, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(arc, lv_color_hex(0x26262c), LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, g_theme.accent, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(arc, lv_color_white(), LV_PART_KNOB);
    lv_obj_set_style_pad_all(arc, 4, LV_PART_KNOB);
    return arc;
}

static void build_big(lv_obj_t *page, widget_t *w)
{
    char ic[6];
    if (!strcmp(w->kind, "climate")) {
        w->arc = page_arc(page, 14, 60);
        lv_obj_add_event_cb(w->arc, climate_changed, LV_EVENT_VALUE_CHANGED, w);
        lv_obj_add_event_cb(w->arc, climate_release, LV_EVENT_RELEASED, w);
        w->ic = label(page, g_fonts.icon34, g_theme.accent);
        lv_label_set_text(w->ic, mdi(w->icon, ic));
        lv_obj_align(w->ic, LV_ALIGN_CENTER, 0, -92);
        w->big = label(page, g_fonts.clock, g_theme.text);
        lv_obj_align(w->big, LV_ALIGN_CENTER, 0, -16);
        w->sub = label(page, g_fonts.text16, g_theme.text_dim);
        lv_obj_align(w->sub, LV_ALIGN_CENTER, 0, 40);
        w->lname = label(page, g_fonts.text20, g_theme.text);
        lv_obj_set_width(w->lname, 260);
        lv_label_set_long_mode(w->lname, LV_LABEL_LONG_DOT);
        lv_obj_align(w->lname, LV_ALIGN_CENTER, 0, 80);
    } else if (!strcmp(w->kind, "media")) {
        w->arc = page_arc(page, 0, 100);
        lv_obj_add_event_cb(w->arc, media_vol, LV_EVENT_RELEASED, w);
        w->lname = label(page, g_fonts.text16, g_theme.text_dim);
        lv_obj_set_width(w->lname, 260);
        lv_label_set_long_mode(w->lname, LV_LABEL_LONG_DOT);
        lv_obj_align(w->lname, LV_ALIGN_CENTER, 0, -110);
        w->big = label(page, g_fonts.text26, g_theme.text);
        lv_obj_set_width(w->big, 300);
        lv_label_set_long_mode(w->big, LV_LABEL_LONG_SCROLL_CIRCULAR);
        lv_obj_align(w->big, LV_ALIGN_CENTER, 0, -60);
        w->sub = label(page, g_fonts.text16, g_theme.text_dim);
        lv_obj_set_width(w->sub, 280);
        lv_label_set_long_mode(w->sub, LV_LABEL_LONG_DOT);
        lv_obj_align(w->sub, LV_ALIGN_CENTER, 0, -24);
        static const char *svc[3] = {"media_previous_track", "media_play_pause", "media_next_track"};
        static const uint32_t icons[3] = {ICON_SKIP_PREVIOUS, ICON_PLAY, ICON_SKIP_NEXT};
        for (int i = 0; i < 3; i++) {
            lv_obj_t *b = lv_button_create(page);
            int sz = i == 1 ? 96 : 72;
            lv_obj_set_size(b, sz, sz);
            lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
            lv_obj_set_style_bg_color(b, i == 1 ? g_theme.accent : lv_color_hex(0x26262c), 0);
            lv_obj_align(b, LV_ALIGN_CENTER, (i - 1) * 104, 56);
            lv_obj_set_user_data(b, (void *)svc[i]);
            lv_obj_t *l = label(b, g_fonts.icon34, i == 1 ? lv_color_hex(0x0a0a0c) : g_theme.text);
            lv_label_set_text(l, mdi(icons[i], ic));
            lv_obj_center(l);
            lv_obj_add_event_cb(b, media_btn, LV_EVENT_CLICKED, w);
            if (i == 1) w->btn_play = b;
        }
    } else {  // weather
        w->ic = label(page, g_fonts.icon56, g_theme.accent);
        lv_obj_align(w->ic, LV_ALIGN_CENTER, 0, -90);
        w->big = label(page, g_fonts.clock, g_theme.text);
        lv_obj_align(w->big, LV_ALIGN_CENTER, 0, -6);
        w->sub = label(page, g_fonts.text16, g_theme.text_dim);
        lv_obj_set_width(w->sub, 320);
        lv_obj_align(w->sub, LV_ALIGN_CENTER, 0, 52);
        w->lname = label(page, g_fonts.text20, g_theme.text);
        lv_obj_set_width(w->lname, 280);
        lv_label_set_long_mode(w->lname, LV_LABEL_LONG_DOT);
        lv_obj_align(w->lname, LV_ALIGN_CENTER, 0, 96);
    }
}

void ha_build_pages(cJSON *pages)
{
    ui_clear_pages();
    s_dial = NULL;
    s_n = 0;
    s_pages = 0;
    cJSON *p;
    cJSON_ArrayForEach(p, pages) {
        cJSON *ws = cJSON_GetObjectItem(p, "widgets");
        int cnt = cJSON_GetArraySize(ws);
        if (!cnt || s_n + cnt > MAX_W) continue;
        lv_obj_t *page = ui_add_page();
        s_pages++;
        lv_obj_t *t = label(page, g_fonts.text16, g_theme.text_dim);
        lv_label_set_text(t, cJSON_GetStringValue(cJSON_GetObjectItem(p, "title")) ?: "");
        lv_obj_align(t, LV_ALIGN_TOP_MID, 0, 26);
        int idx = 0;
        cJSON *wj;
        cJSON_ArrayForEach(wj, ws) {
            widget_t *w = &s_w[s_n++];
            memset(w, 0, sizeof(*w));
            strlcpy(w->e, cJSON_GetStringValue(cJSON_GetObjectItem(wj, "e")) ?: "", sizeof(w->e));
            strlcpy(w->kind, cJSON_GetStringValue(cJSON_GetObjectItem(wj, "k")) ?: "sensor", sizeof(w->kind));
            strlcpy(w->name, cJSON_GetStringValue(cJSON_GetObjectItem(wj, "n")) ?: "", sizeof(w->name));
            cJSON *i = cJSON_GetObjectItem(wj, "i");
            w->icon = cJSON_IsNumber(i) ? (uint32_t)i->valuedouble : ICON_CIRCLE_OUTLINE;
            bool big = !strcmp(w->kind, "climate") || !strcmp(w->kind, "media") || !strcmp(w->kind, "weather");
            if (big) {
                build_big(page, w);
            } else {
                // 1 → one big tile; 2 → side by side; 3 → 2 + 1; 4 → 2×2
                static const int pos[4][4][2] = {
                    {{233, 240}},
                    {{148, 240}, {318, 240}},
                    {{150, 160}, {316, 160}, {233, 326}},
                    {{150, 152}, {316, 152}, {150, 318}, {316, 318}},
                };
                int n = cnt > 4 ? 4 : cnt;
                int sz = n == 1 ? 220 : 156;
                build_tile(page, w, pos[n - 1][idx][0], pos[n - 1][idx][1], sz);
            }
            idx++;
            refresh(w);
        }
    }
    ESP_LOGI(TAG, "%d pages, %d widgets", s_pages, s_n);
    ha_resend_retain();
}

int ha_page_count(void) { return s_pages; }

void ha_resend_retain(void)
{
    const char *ids[MAX_W];
    for (int i = 0; i < s_n; i++) ids[i] = s_w[i].e;
    ws_send_retain(ids, s_n);
}

void ha_on_states(cJSON *states, bool full)
{
    if (!cJSON_IsObject(states)) return;
    if (full || !s_states) {
        cJSON_Delete(s_states);
        s_states = cJSON_CreateObject();
    }
    cJSON *it = states->child;
    while (it) {
        cJSON *next = it->next;
        char e[80];
        strlcpy(e, it->string ? it->string : "", sizeof(e));
        cJSON_DeleteItemFromObject(s_states, e);
        if (cJSON_IsObject(it)) {
            cJSON_DetachItemViaPointer(states, it);
            cJSON_AddItemToObject(s_states, e, it);
        }
        widget_t *w = find(e);
        if (w && !full) refresh(w);
        it = next;
    }
    if (full) refresh_all();
}
