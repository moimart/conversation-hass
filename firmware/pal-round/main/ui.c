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

#define ORB_SIZE 448   // ~96% of the 466 px screen (a multiple of 16 for the JPEG decoder)

pal_theme_t g_theme;
ui_choices_t g_choices;

static lv_obj_t *s_tv, *s_home, *s_box, *s_clockpage, *s_weather_lbl;
static void box_sync(void);
static void box_style(void);
static lv_obj_t *s_clock, *s_date, *s_weather_ic, *s_weather_t, *s_said, *s_reply, *s_status, *s_hint;
static lv_obj_t *s_pair_digits, *s_pair_msg, *s_toast, *s_timer_lbl, *s_shield;
static lv_obj_t *s_vol, *s_bright, *s_info, *s_update_btn;
void ui_settings_open(void);
void ui_settings_close(void);
static void orb_long(lv_event_t *e);
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

// --- page switching: swipes jump straight to the next page (no sliding) -------------

static int col_of(lv_obj_t *tv)
{
    lv_obj_t *act = lv_tileview_get_tile_active(tv);
    int w = lv_obj_get_width(tv);
    return act && w ? lv_obj_get_x(act) / w : 0;
}

static void jump(lv_obj_t *tv, int ncols, lv_dir_t dir)
{
    int c = col_of(tv);
    if (dir == LV_DIR_LEFT && c + 1 < ncols) c++;
    else if (dir == LV_DIR_RIGHT && c > 0) c--;
    else return;
    lv_tileview_set_tile_by_index(tv, c, 0, LV_ANIM_OFF);
    lv_obj_send_event(tv, LV_EVENT_VALUE_CHANGED, NULL);
}

static void make_jumpy(lv_obj_t *tv)
{
    lv_obj_remove_flag(tv, LV_OBJ_FLAG_SCROLLABLE);   // pages don't follow the finger
}

static int s_page_cols;
static lv_obj_t *s_set, *s_set_tv;
static lv_obj_t *s_setup, *s_pair, *s_ota;
extern lv_obj_t *ha_dial_obj(void);
static lv_point_t s_touch0;
static bool s_swipe_armed;
static void dots_cb(lv_event_t *e);

/** Mark a control (dial, slider) whose drags must not turn the page. */
void ui_no_swipe(lv_obj_t *obj) { lv_obj_add_flag(obj, LV_OBJ_FLAG_USER_1); }

static bool on_no_swipe(lv_obj_t *o)
{
    for (; o; o = lv_obj_get_parent(o))
        if (lv_obj_has_flag(o, LV_OBJ_FLAG_USER_1)) return true;
    return false;
}

// Swipe detection: this touch driver only raises PRESSED on the input device
// (no PRESSING), so a 30 ms timer follows the finger. Once it has moved far
// enough sideways, jump one page and ignore the rest of the touch (no click).
static lv_indev_t *s_indev;
static bool s_was_pressed;
static lv_timer_t *s_swipe_timer, *s_anim_timer;

static void swipe_poll(lv_timer_t *t)
{
    if (!s_indev) return;
    bool pressed = lv_indev_get_state(s_indev) == LV_INDEV_STATE_PRESSED;
    lv_point_t p;
    lv_indev_get_point(s_indev, &p);
    if (pressed && !s_was_pressed) {
        s_touch0 = p;
        s_swipe_armed = !on_no_swipe(lv_indev_get_active_obj());
    }
    s_was_pressed = pressed;
    if (!pressed || !s_swipe_armed) return;
    int dx = p.x - s_touch0.x, dy = p.y - s_touch0.y;
    if (LV_ABS(dx) < 60 || LV_ABS(dx) < 2 * LV_ABS(dy)) return;
    s_swipe_armed = false;
    lv_obj_t *tv;
    int n;
    if (s_set && !lv_obj_has_flag(s_set, LV_OBJ_FLAG_HIDDEN)) { tv = s_set_tv; n = 5; }
    else if (s_setup || s_pair || s_ota || ha_dial_obj()) return;
    else { tv = s_tv; n = s_page_cols + 1; }
    ui_activity();
    jump(tv, n, dx < 0 ? LV_DIR_LEFT : LV_DIR_RIGHT);
    if (tv == s_set_tv) dots_cb(NULL);
    lv_indev_wait_release(s_indev);
}

// --- home -------------------------------------------------------------------------

static void orb_tap(lv_event_t *e)
{
    ui_activity();
    app_post(EV_PTT_PRESS);
}

static void build_home(void)
{
    s_home = lv_tileview_add_tile(s_tv, 0, 0, LV_DIR_RIGHT);
    lv_obj_remove_flag(s_home, LV_OBJ_FLAG_SCROLLABLE);

    orb_create(s_home, ORB_SIZE);
    lv_obj_align(orb_obj(), LV_ALIGN_CENTER, -1, -1);   // image at (8,8): even window for direct blits
    lv_obj_add_flag(orb_obj(), LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(orb_obj(), orb_tap, LV_EVENT_SHORT_CLICKED, NULL);
    lv_obj_add_event_cb(orb_obj(), orb_long, LV_EVENT_LONG_PRESSED, NULL);

    s_timer_lbl = mk_label(s_home, g_fonts.clock, lv_color_white(), "");
    lv_obj_align(s_timer_lbl, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_color(s_timer_lbl, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_timer_lbl, LV_OPA_60, 0);
    lv_obj_set_style_radius(s_timer_lbl, 20, 0);
    lv_obj_set_style_pad_hor(s_timer_lbl, 16, 0);
    lv_obj_add_flag(s_timer_lbl, LV_OBJ_FLAG_HIDDEN);

    // the conversation text lives in one rounded "glass" box over the bottom
    // of the orb (kept above y≈410 so the round edge never clips its corners)
    s_box = lv_obj_create(s_home);
    lv_obj_remove_style_all(s_box);
    lv_obj_set_width(s_box, 300);
    lv_obj_set_height(s_box, LV_SIZE_CONTENT);
    lv_obj_align(s_box, LV_ALIGN_BOTTOM_MID, 0, -54);
    lv_obj_set_style_radius(s_box, 22, 0);
    lv_obj_set_style_bg_opa(s_box, LV_OPA_COVER, 0);   // opaque: a translucent box re-blends over every orb frame
    lv_obj_set_style_border_width(s_box, 1, 0);
    lv_obj_set_style_border_opa(s_box, LV_OPA_40, 0);
    lv_obj_set_style_pad_hor(s_box, 16, 0);
    lv_obj_set_style_pad_ver(s_box, 10, 0);
    lv_obj_set_style_pad_row(s_box, 4, 0);
    lv_obj_set_flex_flow(s_box, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_box, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_remove_flag(s_box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(s_box, LV_OBJ_FLAG_CLICKABLE);

    s_said = mk_label(s_box, g_fonts.text16, g_theme.text_dim, "");
    lv_obj_set_width(s_said, LV_PCT(100));
    lv_label_set_long_mode(s_said, LV_LABEL_LONG_DOT);
    s_reply = mk_label(s_box, g_fonts.text16, g_theme.text, "");
    lv_obj_set_width(s_reply, LV_PCT(100));
    lv_obj_set_style_max_height(s_reply, 44, 0);
    lv_label_set_long_mode(s_reply, LV_LABEL_LONG_DOT);
    s_hint = mk_label(s_box, g_fonts.text16, g_theme.text_dim, "Press a button to talk\nDouble-press for a voice note\nHold the orb for settings");
    lv_obj_set_width(s_hint, LV_PCT(100));
    box_sync();
    box_style();

    s_status = mk_label(s_home, g_fonts.icon22, g_theme.text_dim, "");
    lv_obj_align(s_status, LV_ALIGN_TOP_MID, 0, 26);
    lv_obj_set_style_text_font(s_status, g_fonts.icon22, 0);
    lv_obj_add_flag(s_status, LV_OBJ_FLAG_HIDDEN);
}

// The page right of home: clock, date and the weather (the orb has the home
// screen to itself). It survives dashboard reloads (ui_clear_pages).
static void build_clock_page(void)
{
    s_clockpage = lv_tileview_add_tile(s_tv, 1, 0, LV_DIR_LEFT | LV_DIR_RIGHT);
    lv_obj_remove_flag(s_clockpage, LV_OBJ_FLAG_SCROLLABLE);
    s_page_cols = 1;
    s_clock = mk_label(s_clockpage, g_fonts.clock, g_theme.text, "--:--");
    lv_obj_align(s_clock, LV_ALIGN_CENTER, 0, -70);
    s_date = mk_label(s_clockpage, g_fonts.text26, g_theme.accent, "");
    lv_obj_align(s_date, LV_ALIGN_CENTER, 0, -18);
    s_weather_ic = mk_label(s_clockpage, g_fonts.icon56, g_theme.accent, "");
    s_weather_t = mk_label(s_clockpage, g_fonts.text40, g_theme.text, "");
    s_weather_lbl = mk_label(s_clockpage, g_fonts.text16, g_theme.text_dim, "");
    lv_obj_add_flag(s_weather_ic, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_weather_t, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_weather_lbl, LV_OBJ_FLAG_HIDDEN);
}

// --- settings ----------------------------------------------------------------------
//
// A full-screen overlay of swipeable pages (no scrolling lists, no dropdowns):
// opened by long-pressing the orb, closed with the ✕ on every page.

typedef struct { lv_obj_t *val; int idx; int kind; } picker_t;
enum { PK_TMO, PK_DASH, PK_ORB };
static lv_obj_t *s_dots[5];
static picker_t s_pk[3];
static const char *TMO_NAMES[] = {"15 seconds", "30 seconds", "1 minute", "5 minutes", "Never"};
static const uint16_t TMO_VALS[] = {15, 30, 60, 300, 0};

static const char *nth_line(const char *src, int n, char *buf, size_t len)
{
    const char *p = src;
    for (int i = 0; i < n && p; i++) {
        p = strchr(p, '\n');
        if (p) p++;
    }
    if (!p) { buf[0] = 0; return buf; }
    const char *e = strchr(p, '\n');
    size_t l = e ? (size_t)(e - p) : strlen(p);
    if (l >= len) l = len - 1;
    memcpy(buf, p, l);
    buf[l] = 0;
    return buf;
}

static int picker_count(int kind)
{
    if (kind == PK_TMO) return 5;
    if (kind == PK_DASH) return g_choices.dash_n + 1;
    return g_choices.orb_n ? g_choices.orb_n : 1;
}

static void picker_show(picker_t *p)
{
    char b[64];
    if (p->kind == PK_TMO) strlcpy(b, TMO_NAMES[p->idx], sizeof(b));
    else if (p->kind == PK_DASH) {
        if (p->idx == 0) strcpy(b, "None");
        else nth_line(g_choices.dash_opts, p->idx - 1, b, sizeof(b));
    } else {
        if (g_choices.orb_n) nth_line(g_choices.orb_opts, p->idx, b, sizeof(b));
        else strcpy(b, "Follow PAL");
    }
    lv_label_set_text(p->val, b);
}

static void picker_apply(picker_t *p)
{
    if (p->kind == PK_TMO) {
        g_settings.screen_timeout_s = TMO_VALS[p->idx];
    } else if (p->kind == PK_DASH) {
        if (p->idx == 0) strcpy(g_settings.dashboard, "-");
        else strlcpy(g_settings.dashboard, g_choices.dash_ids[p->idx - 1], sizeof(g_settings.dashboard));
        app_post(EV_RELOAD);
    } else if (g_choices.orb_n) {
        strlcpy(g_settings.orb, g_choices.orb_ids[p->idx], sizeof(g_settings.orb));
        app_post(EV_RELOAD);
    }
    app_post(EV_SETTINGS_CHANGED);
}

static void picker_step(lv_event_t *e)
{
    picker_t *p = lv_event_get_user_data(e);
    int dir = (int)(intptr_t)lv_obj_get_user_data(lv_event_get_target(e));
    int n = picker_count(p->kind);
    ui_activity();
    p->idx = (p->idx + dir + n) % n;
    picker_show(p);
    picker_apply(p);
}

static lv_obj_t *round_btn(lv_obj_t *parent, uint32_t icon, int size)
{
    char ic[6];
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, size, size);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(0x26262c), 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_t *l = mk_label(b, g_fonts.icon34, g_theme.text, mdi(icon, ic));
    lv_obj_center(l);
    return b;
}

static void picker_build(lv_obj_t *page, picker_t *p, int kind, int y)
{
    p->kind = kind;
    for (int dir = -1; dir <= 1; dir += 2) {
        lv_obj_t *b = round_btn(page, dir < 0 ? ICON_CHEVRON_LEFT : ICON_CHEVRON_RIGHT, 64);
        lv_obj_align(b, LV_ALIGN_CENTER, dir * 150, y);
        lv_obj_set_user_data(b, (void *)(intptr_t)dir);
        lv_obj_add_event_cb(b, picker_step, LV_EVENT_CLICKED, p);
    }
    p->val = mk_label(page, g_fonts.text20, g_theme.text, "");
    lv_obj_set_width(p->val, 220);
    lv_label_set_long_mode(p->val, LV_LABEL_LONG_DOT);
    lv_obj_align(p->val, LV_ALIGN_CENTER, 0, y);
}

static void slider_cb(lv_event_t *e)
{
    lv_obj_t *t = lv_event_get_target(e);
    ui_activity();
    if (t == s_vol) {
        g_settings.volume = lv_slider_get_value(s_vol);
        audio_set_volume(g_settings.volume);
        audio_beep(1);
    } else {
        g_settings.brightness = lv_slider_get_value(s_bright);
        bsp_display_brightness_set(g_settings.brightness);
    }
    app_post(EV_SETTINGS_CHANGED);
}

static lv_obj_t *slider_row(lv_obj_t *page, uint32_t icon, int lo, int hi, int v, int y)
{
    char ic[6];
    lv_obj_t *l = mk_label(page, g_fonts.icon34, g_theme.accent, mdi(icon, ic));
    lv_obj_align(l, LV_ALIGN_CENTER, -150, y);
    lv_obj_t *s = lv_slider_create(page);
    lv_obj_set_size(s, 250, 16);
    lv_obj_align(s, LV_ALIGN_CENTER, 30, y);
    lv_slider_set_range(s, lo, hi);
    lv_slider_set_value(s, v, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(s, g_theme.accent, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s, lv_color_white(), LV_PART_KNOB);
    lv_obj_set_style_bg_color(s, lv_color_hex(0x2a2a30), LV_PART_MAIN);
    lv_obj_set_ext_click_area(s, 20);
    lv_obj_add_event_cb(s, slider_cb, LV_EVENT_RELEASED, NULL);
    ui_no_swipe(s);
    return s;
}

static void action_cb(lv_event_t *e)
{
    ui_activity();
    const char *a = lv_event_get_user_data(e);
    if (strcmp(a, "ota")) ui_settings_close();
    app_post_str(EV_ACTION, a);
}

static lv_obj_t *action_btn(lv_obj_t *page, uint32_t icon, const char *txt, const char *action, int x, int y)
{
    lv_obj_t *b = round_btn(page, icon, 104);
    lv_obj_align(b, LV_ALIGN_CENTER, x, y);
    lv_obj_t *l = lv_obj_get_child(b, 0);
    lv_obj_align(l, LV_ALIGN_CENTER, 0, -12);
    lv_obj_t *t = mk_label(b, g_fonts.text16, g_theme.text_dim, txt);
    lv_obj_align(t, LV_ALIGN_CENTER, 0, 26);
    lv_obj_add_event_cb(b, action_cb, LV_EVENT_CLICKED, (void *)action);
    return b;
}

static void close_cb(lv_event_t *e) { ui_settings_close(); }

static void dots_cb(lv_event_t *e)
{
    int idx = col_of(s_set_tv);
    for (int i = 0; i < 5; i++)
        lv_obj_set_style_bg_color(s_dots[i], i == idx ? g_theme.accent : lv_color_hex(0x3a3a42), 0);
}

static lv_obj_t *set_page(int col, const char *title)
{
    lv_obj_t *t = lv_tileview_add_tile(s_set_tv, col, 0, LV_DIR_LEFT | LV_DIR_RIGHT);
    lv_obj_remove_flag(t, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *h = mk_label(t, g_fonts.text20, g_theme.text, title);
    lv_obj_align(h, LV_ALIGN_TOP_MID, 0, 46);
    lv_obj_t *x = round_btn(t, ICON_CLOSE, 60);
    lv_obj_align(x, LV_ALIGN_BOTTOM_MID, 0, -26);
    lv_obj_add_event_cb(x, close_cb, LV_EVENT_CLICKED, NULL);
    return t;
}

static void build_settings(void)
{
    s_set = overlay();
    lv_obj_add_flag(s_set, LV_OBJ_FLAG_HIDDEN);
    s_set_tv = lv_tileview_create(s_set);
    lv_obj_set_style_bg_opa(s_set_tv, LV_OPA_TRANSP, 0);
    lv_obj_set_scrollbar_mode(s_set_tv, LV_SCROLLBAR_MODE_OFF);
    lv_obj_add_event_cb(s_set_tv, dots_cb, LV_EVENT_VALUE_CHANGED, NULL);
    make_jumpy(s_set_tv);

    lv_obj_t *p = set_page(0, "Sound & screen");
    s_vol = slider_row(p, ICON_VOLUME_HIGH, 0, 100, g_settings.volume, -70);
    s_bright = slider_row(p, ICON_BRIGHTNESS_6, 5, 100, g_settings.brightness, 0);
    lv_obj_t *l = mk_label(p, g_fonts.text16, g_theme.text_dim, "Screen off after");
    lv_obj_align(l, LV_ALIGN_CENTER, 0, 52);
    picker_build(p, &s_pk[PK_TMO], PK_TMO, 92);

    p = set_page(1, "Home Assistant");
    picker_build(p, &s_pk[PK_DASH], PK_DASH, 0);
    l = mk_label(p, g_fonts.text16, g_theme.text_dim, "Dashboard shown as pages\nright of the home screen");
    lv_obj_align(l, LV_ALIGN_CENTER, 0, 70);

    p = set_page(2, "Orb");
    picker_build(p, &s_pk[PK_ORB], PK_ORB, 0);
    l = mk_label(p, g_fonts.text16, g_theme.text_dim, "\"Follow PAL\" shows the hub's orb");
    lv_obj_align(l, LV_ALIGN_CENTER, 0, 70);

    p = set_page(3, "Device");
    s_update_btn = action_btn(p, ICON_DOWNLOAD, "Update", "ota", -60, -50);
    action_btn(p, ICON_WIFI, "Wi-Fi", "portal", 60, -50);
    action_btn(p, ICON_LINK_VARIANT, "Pair", "repair", -60, 66);
    action_btn(p, ICON_REFRESH, "Restart", "restart", 60, 66);

    p = set_page(4, "About");
    s_info = mk_label(p, g_fonts.text16, g_theme.text_dim, "");
    lv_obj_set_width(s_info, 320);
    lv_label_set_long_mode(s_info, LV_LABEL_LONG_WRAP);
    lv_obj_align(s_info, LV_ALIGN_CENTER, 0, -30);
    lv_obj_t *rst = lv_button_create(p);
    lv_obj_set_size(rst, 220, 44);
    lv_obj_align(rst, LV_ALIGN_CENTER, 0, 78);
    lv_obj_set_style_radius(rst, 22, 0);
    lv_obj_set_style_bg_color(rst, lv_color_hex(0x3a1616), 0);
    lv_obj_t *rl = mk_label(rst, g_fonts.text16, g_theme.text, "Hold to factory reset");
    lv_obj_center(rl);
    lv_obj_add_event_cb(rst, action_cb, LV_EVENT_LONG_PRESSED, (void *)"reset");

    for (int i = 0; i < 5; i++) {
        s_dots[i] = lv_obj_create(s_set);
        lv_obj_remove_style_all(s_dots[i]);
        lv_obj_set_size(s_dots[i], 8, 8);
        lv_obj_set_style_radius(s_dots[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_opa(s_dots[i], LV_OPA_COVER, 0);
        lv_obj_align(s_dots[i], LV_ALIGN_TOP_MID, (i - 2) * 16, 82);
    }
}

void ui_settings_refresh(void)
{
    if (!s_set) return;
    lv_slider_set_value(s_vol, g_settings.volume, LV_ANIM_OFF);
    lv_slider_set_value(s_bright, g_settings.brightness, LV_ANIM_OFF);
    int t = g_settings.screen_timeout_s;
    s_pk[PK_TMO].idx = t == 15 ? 0 : t == 30 ? 1 : t == 60 ? 2 : t == 300 ? 3 : 4;
    s_pk[PK_DASH].idx = 0;
    for (int i = 0; i < g_choices.dash_n; i++)
        if (!strcmp(g_choices.dash_ids[i], g_settings.dashboard)) s_pk[PK_DASH].idx = i + 1;
    s_pk[PK_ORB].idx = 0;
    for (int i = 0; i < g_choices.orb_n; i++)
        if (!strcmp(g_choices.orb_ids[i], g_settings.orb)) s_pk[PK_ORB].idx = i;
    for (int i = 0; i < 3; i++) picker_show(&s_pk[i]);
    lv_obj_t *ul = lv_obj_get_child(s_update_btn, 1);
    lv_label_set_text(ul, g_choices.update[0] ? g_choices.update : "Update");
    lv_obj_set_style_bg_color(s_update_btn, g_choices.update[0] ? g_theme.accent_deep : lv_color_hex(0x26262c), 0);
    int bat = pmu_battery_pct();
    char batt[32] = "USB power";
    if (bat >= 0) snprintf(batt, sizeof(batt), "Battery %d%%%s", bat, pmu_charging() ? ", charging" : "");
    lv_label_set_text_fmt(s_info, "%s\nFirmware %s\n%s\n%s · %d dBm\n%s", pal_device_name(), pal_fw_version(),
                          pal_base(), net_ssid(), net_rssi(), batt);
}

static void orb_sync_active(void);

void ui_settings_open(void)
{
    ui_settings_refresh();
    lv_tileview_set_tile_by_index(s_set_tv, 0, 0, LV_ANIM_OFF);
    dots_cb(NULL);
    lv_obj_remove_flag(s_set, LV_OBJ_FLAG_HIDDEN);
    orb_sync_active();
}

void ui_settings_close(void)
{
    if (s_set) lv_obj_add_flag(s_set, LV_OBJ_FLAG_HIDDEN);
    orb_sync_active();
}


static void orb_long(lv_event_t *e)
{
    ui_activity();
    ui_settings_open();
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
        if (c != s_home && c != s_clockpage) lv_obj_delete(c);
    }
    s_page_cols = 1;
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

// empty lines collapse; the hint only shows when there's no conversation
static void box_sync(void)
{
    bool said = lv_label_get_text(s_said)[0] != 0, reply = lv_label_get_text(s_reply)[0] != 0;
    if (said) lv_obj_remove_flag(s_said, LV_OBJ_FLAG_HIDDEN); else lv_obj_add_flag(s_said, LV_OBJ_FLAG_HIDDEN);
    if (reply) lv_obj_remove_flag(s_reply, LV_OBJ_FLAG_HIDDEN); else lv_obj_add_flag(s_reply, LV_OBJ_FLAG_HIDDEN);
    if (said || reply) lv_obj_add_flag(s_hint, LV_OBJ_FLAG_HIDDEN); else lv_obj_remove_flag(s_hint, LV_OBJ_FLAG_HIDDEN);
}

static void box_style(void)
{
    lv_obj_set_style_bg_color(s_box, lv_color_mix(g_theme.accent, lv_color_hex(0x0c0c10), 24), 0);
    lv_obj_set_style_border_color(s_box, g_theme.accent, 0);
    lv_obj_set_style_shadow_color(s_box, g_theme.accent, 0);
}

void ui_set_state(pal_state_t st)
{
    s_state = st;
    orb_set_state(st);
    if (st == PAL_LISTENING) {
        lv_label_set_text(s_reply, "");
        lv_label_set_text(s_said, "Listening…");
        s_text_set_at = esp_timer_get_time();
    } else if (st == PAL_PROCESSING && !strcmp(lv_label_get_text(s_said), "Listening…")) {
        lv_label_set_text(s_said, "…");
    }
    box_sync();
}

void ui_set_transcript(const char *text)
{
    lv_label_set_text(s_said, text ? text : "");
    s_text_set_at = esp_timer_get_time();
    box_sync();
}

void ui_note_status(const char *line1, const char *line2)
{
    lv_label_set_text(s_said, line1 ? line1 : "");
    lv_label_set_text(s_reply, line2 ? line2 : "");
    s_text_set_at = esp_timer_get_time();
    box_sync();
}

void ui_set_response(const char *text)
{
    lv_label_set_text(s_reply, text ? text : "");
    s_text_set_at = esp_timer_get_time();
    box_sync();
}

void ui_set_weather(const char *cond, double temp, const char *unit, bool show)
{
    if (!show) {
        lv_obj_add_flag(s_weather_ic, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_weather_t, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_weather_lbl, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    char ic[8];
    lv_label_set_text(s_weather_ic, mdi(ha_weather_icon(cond), ic));
    lv_label_set_text_fmt(s_weather_t, "%.0f%s", temp, unit && unit[0] ? unit : "°");
    // "partlycloudy" / "clear-night" → "Partly cloudy" / "Clear night"
    char name[40] = "";
    if (cond) {
        strlcpy(name, !strcmp(cond, "partlycloudy") ? "partly cloudy" : cond, sizeof(name));
        for (char *p = name; *p; p++) if (*p == '-' || *p == '_') *p = ' ';
        if (name[0] >= 'a' && name[0] <= 'z') name[0] -= 32;
    }
    lv_label_set_text(s_weather_lbl, name);
    lv_obj_remove_flag(s_weather_ic, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_weather_t, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_weather_lbl, LV_OBJ_FLAG_HIDDEN);
    lv_obj_update_layout(s_clockpage);
    int w = lv_obj_get_width(s_weather_ic) + 12 + lv_obj_get_width(s_weather_t);
    lv_obj_align(s_weather_ic, LV_ALIGN_CENTER, -w / 2 + lv_obj_get_width(s_weather_ic) / 2, 62);
    lv_obj_align_to(s_weather_t, s_weather_ic, LV_ALIGN_OUT_RIGHT_MID, 12, 0);
    lv_obj_align(s_weather_lbl, LV_ALIGN_CENTER, 0, 112);
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
    orb_sync_active();   // overlays (setup, pairing, update) come and go
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
        box_sync();
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

// The orb only decodes while someone can see it: screen on, home page, no
// overlay on top. Video decoding is the biggest CPU load on the device.
static void orb_sync_active(void)
{
    bool covered = (s_set && !lv_obj_has_flag(s_set, LV_OBJ_FLAG_HIDDEN)) || s_setup || s_pair || s_ota;
    orb_set_active(s_screen_on && !covered && lv_tileview_get_tile_active(s_tv) == s_home);
}

static void tile_changed(lv_event_t *e) { orb_sync_active(); }

// For the orb's direct blits (orb.c): -1 when the orb isn't plainly on screen
// (another page, an overlay, screen off); otherwise the screen rects of the
// things drawn over it, which must still go through LVGL.
int ui_orb_overlays(lv_area_t *out, int max)
{
    bool covered = (s_set && !lv_obj_has_flag(s_set, LV_OBJ_FLAG_HIDDEN)) || s_setup || s_pair || s_ota;
    if (!s_screen_on || covered || lv_tileview_get_tile_active(s_tv) != s_home) return -1;
    lv_obj_t *over[] = {s_box, s_status, s_timer_lbl, s_toast};
    int n = 0;
    for (size_t i = 0; i < sizeof(over) / sizeof(over[0]) && n < max; i++) {
        if (!over[i] || lv_obj_has_flag(over[i], LV_OBJ_FLAG_HIDDEN)) continue;
        lv_obj_get_coords(over[i], &out[n]);
        lv_area_increase(&out[n], 2, 2);   // anti-aliased edges
        n++;
    }
    return n;
}

static void brightness_restore(lv_timer_t *t)
{
    if (s_screen_on) bsp_display_brightness_set(g_settings.brightness);
}

// Screen off = panel in deep standby, LVGL stops drawing (no invalidation),
// its timers slow down (touch still polled, to wake), the orb stops, and
// Wi-Fi drops to a deeper modem sleep. The PAL socket stays connected.
void ui_screen_sleep(void)
{
    if (!s_screen_on) return;
    s_screen_on = false;
    ui_settings_close();
    lv_tileview_set_tile(s_tv, s_home, LV_ANIM_OFF);
    orb_sync_active();
    if (!s_shield) {
        s_shield = lv_obj_create(lv_layer_sys());
        lv_obj_remove_style_all(s_shield);
        lv_obj_set_size(s_shield, LV_PCT(100), LV_PCT(100));
        lv_obj_add_flag(s_shield, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(s_shield, shield_cb, LV_EVENT_ALL, NULL);
    }
    lv_display_t *d = lv_display_get_default();
    lv_display_enable_invalidation(d, false);
    lv_timer_t *rt = lv_display_get_refr_timer(d);
    if (rt) lv_timer_set_period(rt, 500);
    if (s_swipe_timer) lv_timer_pause(s_swipe_timer);
    if (s_anim_timer) lv_timer_pause(s_anim_timer);
    if (s_indev) {
        lv_timer_t *it = lv_indev_get_read_timer(s_indev);
        if (it) lv_timer_set_period(it, 100);
    }
    bsp_display_brightness_set(0);
    bsp_display_power(false);
    net_power_save(true);
    ESP_LOGI(TAG, "screen off");
}

void ui_screen_wake(void)
{
    ui_activity();
    if (s_screen_on) return;
    s_screen_on = true;
    net_power_save(false);
    bsp_display_power(true);   // panel RAM was lost: redraw everything, then light it
    lv_display_t *d = lv_display_get_default();
    lv_display_enable_invalidation(d, true);
    lv_timer_t *rt = lv_display_get_refr_timer(d);
    if (rt) lv_timer_set_period(rt, LV_DEF_REFR_PERIOD);
    if (s_indev) {
        lv_timer_t *it = lv_indev_get_read_timer(s_indev);
        if (it) lv_timer_set_period(it, LV_DEF_REFR_PERIOD);   // LVGL's default read period
    }
    if (s_swipe_timer) lv_timer_resume(s_swipe_timer);
    if (s_anim_timer) lv_timer_resume(s_anim_timer);
    lv_obj_invalidate(lv_screen_active());
    lv_obj_invalidate(lv_layer_top());
    lv_obj_invalidate(lv_layer_sys());
    lv_timer_t *bt = lv_timer_create(brightness_restore, 120, NULL);
    lv_timer_set_repeat_count(bt, 1);
    orb_sync_active();
    ui_tick();
    ESP_LOGI(TAG, "screen on");
}

bool ui_screen_on(void) { return s_screen_on; }

void ui_show_home(void)
{
    lv_tileview_set_tile(s_tv, s_home, LV_ANIM_OFF);
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
    box_style();
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
    make_jumpy(s_tv);
    build_home();
    build_clock_page();
    build_settings();
    s_anim_timer = lv_timer_create(timer_anim, 250, NULL);
    lv_obj_add_event_cb(s_tv, tile_changed, LV_EVENT_VALUE_CHANGED, NULL);
    lv_indev_t *indev = bsp_display_get_input_dev();
    if (indev) {
        lv_indev_add_event_cb(indev, indev_cb, LV_EVENT_PRESSED, NULL);
        s_indev = indev;
        s_swipe_timer = lv_timer_create(swipe_poll, 30, NULL);
    }
    ui_activity();
}
