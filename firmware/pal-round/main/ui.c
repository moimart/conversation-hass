// Screens. A tileview: home (0,0) — clock, weather, the orb, what you said
// and PAL's reply; HA pages to the right (1..n,0); settings below (0,1).
// Overlays on the top layer: setup / pairing / toast / timer / OTA / the
// sleep shield (swallows the touch that wakes the screen).
//
// Every ui_* function expects the caller to hold the display lock (LVGL
// callbacks already do; main.c wraps its calls in pal_lock()).
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include "app.h"
#include "bsp/display.h"
#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "ui";

#define ORB_SIZE 288

pal_theme_t g_theme;
ui_choices_t g_choices;

static lv_obj_t *s_tv, *s_home, *s_settings;
static lv_obj_t *s_clock, *s_date, *s_weather_ic, *s_weather_t, *s_said, *s_reply, *s_status, *s_hint;
static lv_obj_t *s_setup, *s_pair, *s_pair_digits, *s_pair_msg, *s_toast, *s_timer_lbl, *s_ota, *s_shield;
static lv_obj_t *s_vol, *s_bright, *s_tmo, *s_dash, *s_orb_dd, *s_info, *s_update_btn;
static int s_page_cols;
static bool s_screen_on = true;
static int64_t s_last_activity;
static char s_pair_code[8];
static char s_timer_id[48];
static int64_t s_timer_end_ms;
static int64_t s_text_set_at;
static pal_state_t s_state;

// --- helpers --------------------------------------------------------------------

lv_color_t pal_color_hex(const char *hex, lv_color_t fb)
{
    if (!hex || hex[0] != '#' || strlen(hex) < 7) return fb;
    return lv_color_hex((uint32_t)strtol(hex + 1, NULL, 16));
}

static lv_obj_t *mk_label(lv_obj_t *p, const lv_font_t *f, lv_color_t c, const char *txt)
{
    lv_obj_t *l = lv_label_create(p);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, c, 0);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(l, txt ? txt : "");
    return l;
}

static lv_obj_t *overlay(void)
{
    lv_obj_t *o = lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(o);
    lv_obj_set_size(o, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(o, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_add_flag(o, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

void ui_activity(void) { s_last_activity = esp_timer_get_time(); }

static void indev_cb(lv_event_t *e) { ui_activity(); }

// --- home -------------------------------------------------------------------------

static void orb_tap(lv_event_t *e)
{
    ui_activity();
    app_post(EV_PTT_PRESS);
}

static void build_home(void)
{
    s_home = lv_tileview_add_tile(s_tv, 0, 0, LV_DIR_RIGHT | LV_DIR_BOTTOM);
    lv_obj_remove_flag(s_home, LV_OBJ_FLAG_SCROLLABLE);

    s_clock = mk_label(s_home, g_fonts.text40, g_theme.text, "--:--");
    lv_obj_align(s_clock, LV_ALIGN_TOP_MID, 0, 22);
    s_date = mk_label(s_home, g_fonts.text16, g_theme.accent, "");
    lv_obj_align(s_date, LV_ALIGN_TOP_MID, 0, 70);

    s_weather_ic = mk_label(s_home, g_fonts.icon22, g_theme.accent, "");
    s_weather_t = mk_label(s_home, g_fonts.text16, g_theme.text, "");
    lv_obj_add_flag(s_weather_ic, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_weather_t, LV_OBJ_FLAG_HIDDEN);

    orb_create(s_home, ORB_SIZE);
    lv_obj_align(orb_obj(), LV_ALIGN_CENTER, 0, 12);
    lv_obj_add_flag(orb_obj(), LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(orb_obj(), orb_tap, LV_EVENT_SHORT_CLICKED, NULL);

    s_timer_lbl = mk_label(s_home, g_fonts.clock, lv_color_white(), "");
    lv_obj_align(s_timer_lbl, LV_ALIGN_CENTER, 0, 12);
    lv_obj_set_style_bg_color(s_timer_lbl, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_timer_lbl, LV_OPA_60, 0);
    lv_obj_set_style_radius(s_timer_lbl, 20, 0);
    lv_obj_set_style_pad_hor(s_timer_lbl, 16, 0);
    lv_obj_add_flag(s_timer_lbl, LV_OBJ_FLAG_HIDDEN);

    s_said = mk_label(s_home, g_fonts.text16, g_theme.text_dim, "");
    lv_obj_set_width(s_said, 300);
    lv_label_set_long_mode(s_said, LV_LABEL_LONG_DOT);
    lv_obj_align(s_said, LV_ALIGN_BOTTOM_MID, 0, -72);
    s_reply = mk_label(s_home, g_fonts.text16, g_theme.text, "");
    lv_obj_set_width(s_reply, 270);
    lv_obj_set_height(s_reply, 44);
    lv_label_set_long_mode(s_reply, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_align(s_reply, LV_ALIGN_BOTTOM_MID, 0, -24);

    s_hint = mk_label(s_home, g_fonts.text16, g_theme.text_dim, "Press a button to talk");
    lv_obj_align(s_hint, LV_ALIGN_BOTTOM_MID, 0, -40);

    s_status = mk_label(s_home, g_fonts.icon22, g_theme.text_dim, "");
    lv_obj_align(s_status, LV_ALIGN_TOP_MID, 0, 100);
    lv_obj_set_style_text_font(s_status, g_fonts.icon22, 0);
    lv_obj_add_flag(s_status, LV_OBJ_FLAG_HIDDEN);
}

// --- settings ----------------------------------------------------------------------

static void set_changed(lv_event_t *e)
{
    lv_obj_t *t = lv_event_get_target(e);
    ui_activity();
    if (t == s_vol) {
        g_settings.volume = lv_slider_get_value(s_vol);
        audio_set_volume(g_settings.volume);
    } else if (t == s_bright) {
        g_settings.brightness = lv_slider_get_value(s_bright);
        bsp_display_brightness_set(g_settings.brightness);
    } else if (t == s_tmo) {
        static const uint16_t v[] = {15, 30, 60, 300, 0};
        g_settings.screen_timeout_s = v[lv_dropdown_get_selected(s_tmo) % 5];
    } else if (t == s_dash) {
        int i = lv_dropdown_get_selected(s_dash);
        if (i == 0) strcpy(g_settings.dashboard, "-");
        else if (i - 1 < g_choices.dash_n) strlcpy(g_settings.dashboard, g_choices.dash_ids[i - 1], sizeof(g_settings.dashboard));
        app_post(EV_RELOAD);
    } else if (t == s_orb_dd) {
        int i = lv_dropdown_get_selected(s_orb_dd);
        if (i < g_choices.orb_n) strlcpy(g_settings.orb, g_choices.orb_ids[i], sizeof(g_settings.orb));
        app_post(EV_RELOAD);
    }
    app_post(EV_SETTINGS_CHANGED);
}

static void action_cb(lv_event_t *e)
{
    ui_activity();
    app_post_str(EV_ACTION, lv_event_get_user_data(e));
}

static lv_obj_t *row_label(lv_obj_t *p, const char *txt)
{
    lv_obj_t *l = mk_label(p, g_fonts.text16, g_theme.text_dim, txt);
    lv_obj_set_width(l, LV_PCT(100));
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_LEFT, 0);
    return l;
}

static lv_obj_t *slider(lv_obj_t *p, int lo, int hi, int v)
{
    lv_obj_t *s = lv_slider_create(p);
    lv_obj_set_width(s, LV_PCT(92));
    lv_obj_set_height(s, 14);
    lv_slider_set_range(s, lo, hi);
    lv_slider_set_value(s, v, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(s, g_theme.accent, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s, g_theme.accent, LV_PART_KNOB);
    lv_obj_set_style_bg_color(s, lv_color_hex(0x2a2a30), LV_PART_MAIN);
    lv_obj_add_event_cb(s, set_changed, LV_EVENT_RELEASED, NULL);
    lv_obj_add_flag(s, LV_OBJ_FLAG_SCROLL_ON_FOCUS);
    return s;
}

static lv_obj_t *dropdown(lv_obj_t *p)
{
    lv_obj_t *d = lv_dropdown_create(p);
    lv_obj_set_width(d, LV_PCT(100));
    lv_obj_set_style_text_font(d, g_fonts.text16, 0);
    lv_obj_set_style_bg_color(d, lv_color_hex(0x1c1c22), 0);
    lv_obj_set_style_text_color(d, g_theme.text, 0);
    lv_obj_set_style_border_width(d, 0, 0);
    lv_obj_t *list = lv_dropdown_get_list(d);
    lv_obj_set_style_text_font(list, g_fonts.text16, 0);
    lv_obj_set_style_bg_color(list, lv_color_hex(0x1c1c22), 0);
    lv_obj_set_style_text_color(list, g_theme.text, 0);
    lv_obj_set_style_max_height(list, 220, 0);
    lv_obj_add_event_cb(d, set_changed, LV_EVENT_VALUE_CHANGED, NULL);
    return d;
}

static lv_obj_t *button(lv_obj_t *p, const char *txt, const char *action, lv_color_t bg)
{
    lv_obj_t *b = lv_button_create(p);
    lv_obj_set_width(b, LV_PCT(100));
    lv_obj_set_height(b, 50);
    lv_obj_set_style_radius(b, 25, 0);
    lv_obj_set_style_bg_color(b, bg, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_t *l = mk_label(b, g_fonts.text16, g_theme.text, txt);
    lv_obj_center(l);
    lv_obj_add_event_cb(b, action_cb, LV_EVENT_CLICKED, (void *)action);
    return b;
}

static void build_settings(void)
{
    s_settings = lv_tileview_add_tile(s_tv, 0, 1, LV_DIR_TOP);
    lv_obj_t *col = lv_obj_create(s_settings);
    lv_obj_remove_style_all(col);
    lv_obj_set_size(col, 320, LV_PCT(100));
    lv_obj_align(col, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_top(col, 70, 0);
    lv_obj_set_style_pad_bottom(col, 110, 0);
    lv_obj_set_style_pad_row(col, 10, 0);
    lv_obj_set_scroll_dir(col, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(col, LV_SCROLLBAR_MODE_OFF);

    lv_obj_t *title = mk_label(col, g_fonts.text26, g_theme.text, "Settings");
    lv_obj_set_width(title, LV_PCT(100));
    row_label(col, "Volume");
    s_vol = slider(col, 0, 100, g_settings.volume);
    row_label(col, "Brightness");
    s_bright = slider(col, 5, 100, g_settings.brightness);
    row_label(col, "Screen off after");
    s_tmo = dropdown(col);
    lv_dropdown_set_options(s_tmo, "15 seconds\n30 seconds\n1 minute\n5 minutes\nNever");
    row_label(col, "Home Assistant pages");
    s_dash = dropdown(col);
    row_label(col, "Orb");
    s_orb_dd = dropdown(col);
    s_update_btn = button(col, "Check for update", "ota", lv_color_hex(0x26262c));
    button(col, "Wi-Fi setup", "portal", lv_color_hex(0x26262c));
    button(col, "Pair again", "repair", lv_color_hex(0x26262c));
    button(col, "Restart", "restart", lv_color_hex(0x26262c));
    s_info = mk_label(col, g_fonts.text16, g_theme.text_dim, "");
    lv_obj_set_width(s_info, LV_PCT(100));
    lv_label_set_long_mode(s_info, LV_LABEL_LONG_WRAP);
    button(col, "Reset to factory", "reset", lv_color_hex(0x3a1616));
}

void ui_settings_refresh(void)
{
    if (!s_settings) return;
    lv_slider_set_value(s_vol, g_settings.volume, LV_ANIM_OFF);
    lv_slider_set_value(s_bright, g_settings.brightness, LV_ANIM_OFF);
    int t = g_settings.screen_timeout_s;
    lv_dropdown_set_selected(s_tmo, t == 15 ? 0 : t == 30 ? 1 : t == 60 ? 2 : t == 300 ? 3 : 4);
    char opts[600];
    snprintf(opts, sizeof(opts), "None%s%s", g_choices.dash_n ? "\n" : "", g_choices.dash_opts);
    lv_dropdown_set_options(s_dash, opts);
    int sel = 0;
    for (int i = 0; i < g_choices.dash_n; i++)
        if (!strcmp(g_choices.dash_ids[i], g_settings.dashboard)) sel = i + 1;
    lv_dropdown_set_selected(s_dash, sel);
    lv_dropdown_set_options(s_orb_dd, g_choices.orb_n ? g_choices.orb_opts : "Follow PAL");
    sel = 0;
    for (int i = 0; i < g_choices.orb_n; i++)
        if (!strcmp(g_choices.orb_ids[i], g_settings.orb)) sel = i;
    lv_dropdown_set_selected(s_orb_dd, sel);
    lv_obj_t *ul = lv_obj_get_child(s_update_btn, 0);
    if (g_choices.update[0]) {
        lv_label_set_text_fmt(ul, "Update to %s", g_choices.update);
        lv_obj_set_style_bg_color(s_update_btn, g_theme.accent_deep, 0);
    } else {
        lv_label_set_text(ul, "Check for update");
        lv_obj_set_style_bg_color(s_update_btn, lv_color_hex(0x26262c), 0);
    }
    int bat = pmu_battery_pct();
    char batt[32] = "USB power";
    if (bat >= 0) snprintf(batt, sizeof(batt), "Battery %d%%%s", bat, pmu_charging() ? " (charging)" : "");
    lv_label_set_text_fmt(s_info, "%s\nFirmware %s\n%s\nWi-Fi %s (%d dBm)\n%s", pal_device_name(), pal_fw_version(),
                          pal_base(), g_settings.wifi_ssid, net_rssi(), batt);
}

// --- pages --------------------------------------------------------------------------

lv_obj_t *ui_add_page(void)
{
    s_page_cols++;
    lv_obj_t *t = lv_tileview_add_tile(s_tv, s_page_cols, 0, LV_DIR_LEFT | LV_DIR_RIGHT);
    lv_obj_remove_flag(t, LV_OBJ_FLAG_SCROLLABLE);
    return t;
}

void ui_clear_pages(void)
{
    lv_tileview_set_tile(s_tv, s_home, LV_ANIM_OFF);
    for (int i = (int)lv_obj_get_child_count(s_tv) - 1; i >= 0; i--) {
        lv_obj_t *c = lv_obj_get_child(s_tv, i);
        if (c != s_home && c != s_settings) lv_obj_delete(c);
    }
    s_page_cols = 0;
}

// --- overlays -------------------------------------------------------------------------

void ui_show_setup(const char *title, const char *body, const char *qr)
{
    if (s_setup) { lv_obj_delete(s_setup); s_setup = NULL; }
    if (!title) return;
    s_setup = overlay();
    lv_obj_t *t = mk_label(s_setup, g_fonts.text26, g_theme.text, title);
    lv_obj_align(t, LV_ALIGN_TOP_MID, 0, qr ? 48 : 120);
    lv_obj_t *b = mk_label(s_setup, g_fonts.text16, g_theme.text_dim, body);
    lv_obj_set_width(b, 330);
    lv_label_set_long_mode(b, LV_LABEL_LONG_WRAP);
    if (qr) {
        lv_obj_t *q = lv_qrcode_create(s_setup);
        lv_qrcode_set_size(q, 170);
        lv_qrcode_set_dark_color(q, lv_color_black());
        lv_qrcode_set_light_color(q, lv_color_white());
        lv_obj_set_style_border_color(q, lv_color_white(), 0);
        lv_obj_set_style_border_width(q, 6, 0);
        lv_qrcode_update(q, qr, strlen(qr));
        lv_obj_align(q, LV_ALIGN_CENTER, 0, -20);
        lv_obj_align(b, LV_ALIGN_BOTTOM_MID, 0, -50);
    } else {
        lv_obj_align(b, LV_ALIGN_CENTER, 0, 30);
    }
}

static void pair_key(lv_event_t *e)
{
    lv_obj_t *m = lv_event_get_target(e);
    const char *k = lv_buttonmatrix_get_button_text(m, lv_buttonmatrix_get_selected_button(m));
    if (!k) return;
    size_t n = strlen(s_pair_code);
    if (!strcmp(k, "Del")) {
        if (n) s_pair_code[n - 1] = 0;
    } else if (!strcmp(k, "OK")) {
        if (n == 6) app_post_str(EV_PAIR_CODE, s_pair_code);
    } else if (n < 6) {
        s_pair_code[n] = k[0];
        s_pair_code[n + 1] = 0;
        if (n + 1 == 6) app_post_str(EV_PAIR_CODE, s_pair_code);
    }
    char shown[16] = "";
    for (int i = 0; i < 6; i++) {
        size_t l = strlen(shown);
        shown[l] = s_pair_code[i] ? s_pair_code[i] : '_';
        shown[l + 1] = i == 2 ? ' ' : 0;
        if (i == 2) shown[l + 2] = 0;
    }
    lv_label_set_text(s_pair_digits, shown);
}

void ui_set_pair_message(const char *msg)
{
    if (s_pair_msg) lv_label_set_text(s_pair_msg, msg ? msg : "");
}

void ui_show_pairing(bool show, const char *err)
{
    if (!show) {
        if (s_pair) { lv_obj_delete(s_pair); s_pair = NULL; s_pair_msg = NULL; }
        return;
    }
    if (s_pair) {
        s_pair_code[0] = 0;
        lv_label_set_text(s_pair_digits, "_ _ _  _ _ _");
        ui_set_pair_message(err);
        return;
    }
    s_pair_code[0] = 0;
    s_pair = overlay();
    lv_obj_t *t = mk_label(s_pair, g_fonts.text20, g_theme.text, "Pair with PAL");
    lv_obj_align(t, LV_ALIGN_TOP_MID, 0, 34);
    s_pair_msg = mk_label(s_pair, g_fonts.text16, g_theme.text_dim, err ? err : "Enter the code on the hub");
    lv_obj_set_width(s_pair_msg, 300);
    lv_obj_align(s_pair_msg, LV_ALIGN_TOP_MID, 0, 64);
    s_pair_digits = mk_label(s_pair, g_fonts.text40, g_theme.accent, "_ _ _  _ _ _");
    lv_obj_align(s_pair_digits, LV_ALIGN_TOP_MID, 0, 92);
    static const char *map[] = {"1", "2", "3", "\n", "4", "5", "6", "\n", "7", "8", "9", "\n", "Del", "0", "OK", ""};
    lv_obj_t *m = lv_buttonmatrix_create(s_pair);
    lv_buttonmatrix_set_map(m, map);
    lv_obj_set_size(m, 270, 250);
    lv_obj_align(m, LV_ALIGN_BOTTOM_MID, 0, -40);
    lv_obj_set_style_bg_opa(m, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(m, 0, 0);
    lv_obj_set_style_text_font(m, g_fonts.text26, LV_PART_ITEMS);
    lv_obj_set_style_bg_color(m, lv_color_hex(0x1c1c22), LV_PART_ITEMS);
    lv_obj_set_style_text_color(m, g_theme.text, LV_PART_ITEMS);
    lv_obj_set_style_radius(m, 28, LV_PART_ITEMS);
    lv_obj_add_event_cb(m, pair_key, LV_EVENT_VALUE_CHANGED, NULL);
}

static void toast_hide(lv_timer_t *t)
{
    if (s_toast) lv_obj_add_flag(s_toast, LV_OBJ_FLAG_HIDDEN);
    lv_timer_delete(t);
}

void ui_toast(const char *text)
{
    if (!s_toast) {
        s_toast = mk_label(lv_layer_top(), g_fonts.text16, lv_color_white(), "");
        lv_obj_set_style_bg_color(s_toast, lv_color_hex(0x2a2a30), 0);
        lv_obj_set_style_bg_opa(s_toast, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(s_toast, 18, 0);
        lv_obj_set_style_pad_hor(s_toast, 16, 0);
        lv_obj_set_style_pad_ver(s_toast, 8, 0);
        lv_obj_align(s_toast, LV_ALIGN_BOTTOM_MID, 0, -30);
    }
    lv_label_set_text(s_toast, text);
    lv_obj_remove_flag(s_toast, LV_OBJ_FLAG_HIDDEN);
    lv_timer_create(toast_hide, 2200, NULL);
}

void ui_ota_progress(int pct)
{
    if (pct < 0) {
        if (s_ota) { lv_obj_delete(s_ota); s_ota = NULL; }
        return;
    }
    if (!s_ota) {
        s_ota = overlay();
        lv_obj_t *arc = lv_arc_create(s_ota);
        lv_obj_set_size(arc, 380, 380);
        lv_obj_center(arc);
        lv_arc_set_rotation(arc, 270);
        lv_arc_set_bg_angles(arc, 0, 360);
        lv_obj_remove_style(arc, NULL, LV_PART_KNOB);
        lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_arc_color(arc, g_theme.accent, LV_PART_INDICATOR);
        lv_obj_t *l = mk_label(s_ota, g_fonts.text26, g_theme.text, "Updating…");
        lv_obj_center(l);
    }
    lv_arc_set_value(lv_obj_get_child(s_ota, 0), pct);
}

// --- conversation --------------------------------------------------------------------

void ui_set_state(pal_state_t st)
{
    s_state = st;
    orb_set_state(st);
    if (st == PAL_LISTENING) {
        lv_label_set_text(s_reply, "");
        lv_label_set_text(s_said, "Listening…");
        lv_obj_add_flag(s_hint, LV_OBJ_FLAG_HIDDEN);
        s_text_set_at = esp_timer_get_time();
    } else if (st == PAL_PROCESSING && !strcmp(lv_label_get_text(s_said), "Listening…")) {
        lv_label_set_text(s_said, "…");
    }
}

void ui_set_transcript(const char *text)
{
    lv_label_set_text(s_said, text ? text : "");
    lv_obj_add_flag(s_hint, LV_OBJ_FLAG_HIDDEN);
    s_text_set_at = esp_timer_get_time();
}

void ui_set_response(const char *text)
{
    lv_label_set_text(s_reply, text ? text : "");
    lv_obj_add_flag(s_hint, LV_OBJ_FLAG_HIDDEN);
    s_text_set_at = esp_timer_get_time();
}

void ui_set_weather(const char *cond, double temp, const char *unit, bool show)
{
    if (!show) {
        lv_obj_add_flag(s_weather_ic, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_weather_t, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    char ic[6];
    lv_label_set_text(s_weather_ic, mdi(ha_weather_icon(cond), ic));
    lv_label_set_text_fmt(s_weather_t, "%.0f%s", temp, unit && unit[0] ? unit : "°");
    lv_obj_remove_flag(s_weather_ic, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_weather_t, LV_OBJ_FLAG_HIDDEN);
    lv_obj_align(s_weather_ic, LV_ALIGN_TOP_MID, -22, 92);
    lv_obj_align_to(s_weather_t, s_weather_ic, LV_ALIGN_OUT_RIGHT_MID, 6, 0);
}

void ui_set_status(void)
{
    char a[6], b[6], txt[24] = "";
    int bat = pmu_battery_pct();
    bool online = ws_connected();
    if (!online) strcat(txt, mdi(ICON_WIFI_OFF, a));
    if (bat >= 0 && (bat <= 20 || pmu_charging())) {
        strcat(txt, mdi(pmu_charging() ? ICON_BATTERY_CHARGING : ICON_BATTERY_ALERT, b));
    }
    lv_label_set_text(s_status, txt);
    if (txt[0]) lv_obj_remove_flag(s_status, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_status, LV_OBJ_FLAG_HIDDEN);
    lv_obj_align(s_status, LV_ALIGN_BOTTOM_MID, 0, -6);
}

void ui_timer_countdown(const char *id, int64_t ends_at_ms)
{
    strlcpy(s_timer_id, id ? id : "", sizeof(s_timer_id));
    s_timer_end_ms = ends_at_ms;
}

void ui_timer_cancel(const char *id)
{
    if (!id || !strcmp(id, s_timer_id)) {
        s_timer_id[0] = 0;
        s_timer_end_ms = 0;
        lv_obj_add_flag(s_timer_lbl, LV_OBJ_FLAG_HIDDEN);
    }
}

static void timer_anim(lv_timer_t *t)
{
    if (!s_timer_end_ms) return;
    struct timeval tv;
    gettimeofday(&tv, NULL);
    int64_t now = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
    int64_t left = s_timer_end_ms - now;
    if (left <= 0) {
        ui_timer_cancel(NULL);
        return;
    }
    lv_label_set_text_fmt(s_timer_lbl, "%d", (int)((left + 999) / 1000));
    lv_obj_remove_flag(s_timer_lbl, LV_OBJ_FLAG_HIDDEN);
}

void ui_tick(void)
{
    if (net_time_valid()) {
        time_t now = time(NULL);
        struct tm tm;
        localtime_r(&now, &tm);
        char b[32];
        strftime(b, sizeof(b), "%H:%M", &tm);
        lv_label_set_text(s_clock, b);
        strftime(b, sizeof(b), "%a %e %b", &tm);
        lv_label_set_text(s_date, b);
    }
    // the conversation text fades back to the hint after 30 s of quiet
    if (s_state == PAL_IDLE && s_text_set_at && esp_timer_get_time() - s_text_set_at > 30000000LL) {
        lv_label_set_text(s_said, "");
        lv_label_set_text(s_reply, "");
        lv_obj_remove_flag(s_hint, LV_OBJ_FLAG_HIDDEN);
        s_text_set_at = 0;
    }
    ui_set_status();
    // screen timeout (never while something is happening)
    if (s_screen_on && g_settings.screen_timeout_s && s_state == PAL_IDLE && !audio_playing() && !s_pair && !s_setup &&
        !s_ota && esp_timer_get_time() - s_last_activity > (int64_t)g_settings.screen_timeout_s * 1000000LL) {
        ui_screen_sleep();
    }
}

// --- screen power ------------------------------------------------------------------------

static void shield_cb(lv_event_t *e)
{
    lv_event_code_t c = lv_event_get_code(e);
    if (c == LV_EVENT_PRESSED) ui_screen_wake();
    if (c == LV_EVENT_RELEASED && s_shield) {
        lv_obj_delete_async(s_shield);
        s_shield = NULL;
    }
}

void ui_screen_sleep(void)
{
    if (!s_screen_on) return;
    s_screen_on = false;
    orb_set_active(false);
    bsp_display_brightness_set(0);
    if (!s_shield) {
        s_shield = lv_obj_create(lv_layer_sys());
        lv_obj_remove_style_all(s_shield);
        lv_obj_set_size(s_shield, LV_PCT(100), LV_PCT(100));
        lv_obj_add_flag(s_shield, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(s_shield, shield_cb, LV_EVENT_ALL, NULL);
    }
    lv_tileview_set_tile(s_tv, s_home, LV_ANIM_OFF);
    ESP_LOGI(TAG, "screen off");
}

void ui_screen_wake(void)
{
    ui_activity();
    if (s_screen_on) return;
    s_screen_on = true;
    orb_set_active(true);
    bsp_display_brightness_set(g_settings.brightness);
    ui_tick();
}

bool ui_screen_on(void) { return s_screen_on; }

void ui_show_home(void)
{
    lv_tileview_set_tile(s_tv, s_home, LV_ANIM_ON);
}

// --- theme / init ----------------------------------------------------------------------------

void ui_apply_theme(void)
{
    lv_obj_set_style_text_color(s_clock, g_theme.text, 0);
    lv_obj_set_style_text_color(s_date, g_theme.accent, 0);
    lv_obj_set_style_text_color(s_weather_ic, g_theme.accent, 0);
    lv_obj_set_style_text_color(s_weather_t, g_theme.text, 0);
    lv_obj_set_style_text_color(s_said, g_theme.text_dim, 0);
    lv_obj_set_style_text_color(s_reply, g_theme.text, 0);
    lv_obj_set_style_text_color(s_hint, g_theme.text_dim, 0);
    orb_apply_theme();
}

void ui_init(void)
{
    g_theme = (pal_theme_t){
        .bg = lv_color_black(), .text = lv_color_hex(0xf2f2f5), .text_dim = lv_color_hex(0x8a8a96),
        .accent = lv_color_hex(0xff8050), .accent_deep = lv_color_hex(0x8a3418),
    };
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    s_tv = lv_tileview_create(scr);
    lv_obj_set_style_bg_opa(s_tv, LV_OPA_TRANSP, 0);
    lv_obj_set_scrollbar_mode(s_tv, LV_SCROLLBAR_MODE_OFF);
    build_home();
    build_settings();
    lv_timer_create(timer_anim, 250, NULL);
    lv_indev_t *indev = bsp_display_get_input_dev();
    if (indev) lv_indev_add_event_cb(indev, indev_cb, LV_EVENT_PRESSED, NULL);
    ui_activity();
}
