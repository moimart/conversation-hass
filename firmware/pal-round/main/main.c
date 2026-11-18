// PAL Round — app task + boot.
//
// Flow: Wi-Fi (or the setup portal) → find PAL (saved / mDNS) → pair (code
// typed on the round keypad) → load info/orb/pages → /ws/ui. A turn:
// press BOOT (or PWR) → record (VAD) → /api/satellite/stt → /api/command
// → the server answers on the socket (state, response, tts_play) → play.
#include <math.h>
#include <string.h>
#include "app.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "bsp/display.h"
#include "bsp/esp-bsp.h"
#include "driver/gpio.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_pm.h"
#include "freertos/stream_buffer.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "pal";

#define BOOT_BTN GPIO_NUM_0

static QueueHandle_t s_q;
static pal_state_t s_state = PAL_IDLE;
static bool s_loaded;          // info/orb/pages fetched for this connection
static bool s_pairing;
static int64_t s_ptt_down_us;
static int s_ticks;
static char s_devname[32];

void console_start(void);

// --- small helpers -------------------------------------------------------------------

bool pal_lock(void) { return bsp_display_lock(5000) == ESP_OK; }
void pal_unlock(void) { bsp_display_unlock(); }
void *pal_psram_malloc(size_t n) { return heap_caps_malloc(n, MALLOC_CAP_SPIRAM); }
const char *pal_device_name(void) { return s_devname; }
const char *pal_fw_version(void) { return esp_app_get_description()->version; }

void app_post(app_event_type_t type)
{
    app_event_t e = {.type = type};
    if (s_q) xQueueSend(s_q, &e, 0);
}

void app_post_json(cJSON *json)
{
    app_event_t e = {.type = EV_WS_MSG, .json = json};
    if (!s_q || xQueueSend(s_q, &e, pdMS_TO_TICKS(50)) != pdTRUE) cJSON_Delete(json);
}

void app_post_str(app_event_type_t type, const char *s)
{
    app_event_t e = {.type = type};
    strlcpy(e.str, s ? s : "", sizeof(e.str));
    if (s_q) xQueueSend(s_q, &e, 0);
}

static void set_state(pal_state_t st)
{
    s_state = st;
    if (pal_lock()) {
        ui_set_state(st);
        pal_unlock();
    }
}

static void toast(const char *t)
{
    if (pal_lock()) {
        ui_toast(t);
        pal_unlock();
    }
}

static void ble_pair_begin(const char *from);

static void setup_screen(const char *title, const char *body, const char *qr)
{
    if (pal_lock()) {
        ui_show_setup(title, body, qr);
        pal_unlock();
    }
}

// --- loading ---------------------------------------------------------------------------

static void apply_info(cJSON *info)
{
    net_set_tz(cJSON_GetStringValue(cJSON_GetObjectItem(info, "tz_posix")));
    cJSON *c = cJSON_GetObjectItem(cJSON_GetObjectItem(info, "theme"), "colors");
    g_theme.accent = pal_color_hex(cJSON_GetStringValue(cJSON_GetObjectItem(c, "accent")), lv_color_hex(0xff8050));
    g_theme.accent_deep = pal_color_hex(cJSON_GetStringValue(cJSON_GetObjectItem(c, "accent_deep")),
                                        lv_color_darken(g_theme.accent, LV_OPA_50));
    // text stays light: the AMOLED background is always black (light themes'
    // dark text would vanish), so only take a theme's text colour if it's light.
    lv_color_t txt = pal_color_hex(cJSON_GetStringValue(cJSON_GetObjectItem(c, "text")), lv_color_hex(0xf2f2f5));
    if (lv_color_luminance(txt) > 140) g_theme.text = txt;
    cJSON *fw = cJSON_GetObjectItem(info, "firmware");
    const char *ver = cJSON_GetStringValue(cJSON_GetObjectItem(fw, "version"));
    g_choices.update[0] = 0;
    if (ver && strcmp(ver, pal_fw_version()) != 0) strlcpy(g_choices.update, ver, sizeof(g_choices.update));
    if (pal_lock()) {
        ui_apply_theme();
        pal_unlock();
    }
}

static void load_orb(void)
{
    char path[160];
    snprintf(path, sizeof(path), "/api/device/orb?size=448&fps=12&orb=%s", g_settings.orb[0] ? g_settings.orb : "follow");
    cJSON *m = pal_get_json(path, 90000);   // first render of a clip can take a while
    if (m) {
        orb_load_manifest(m);
        cJSON_Delete(m);
    }
}

static void load_pages(void)
{
    cJSON *pages = NULL;
    if (strcmp(g_settings.dashboard, "-") != 0) {
        char path[160];
        snprintf(path, sizeof(path), "/api/device/pages?dashboard=%s",
                 strcmp(g_settings.dashboard, "_default") ? g_settings.dashboard : "");
        cJSON *r = pal_get_json(path, 20000);
        if (r) {
            pages = cJSON_DetachItemFromObject(r, "pages");
            cJSON_Delete(r);
        }
    }
    if (pal_lock()) {
        cJSON *empty = cJSON_CreateArray();
        ha_build_pages(pages ? pages : empty);
        cJSON_Delete(empty);
        pal_unlock();
    }
    cJSON_Delete(pages);
}

static void load_choices(void)
{
    cJSON *d = pal_get_json("/api/ha/dashboards", 10000);
    g_choices.dash_n = 0;
    g_choices.dash_opts[0] = 0;
    cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItem(d, "dashboards")) {
        if (g_choices.dash_n >= 16) break;
        const char *t = cJSON_GetStringValue(cJSON_GetObjectItem(it, "title"));
        const char *u = cJSON_GetStringValue(cJSON_GetObjectItem(it, "url_path"));
        size_t l = strlen(g_choices.dash_opts);
        snprintf(g_choices.dash_opts + l, sizeof(g_choices.dash_opts) - l, "%s%s", l ? "\n" : "", t ? t : "?");
        // HA's default dashboard has no url_path; "" already means "not chosen yet" here
        strlcpy(g_choices.dash_ids[g_choices.dash_n++], u && u[0] ? u : "_default", 64);
    }
    cJSON_Delete(d);
    // default: a dashboard called "pal…" if the user hasn't picked one
    if (!g_settings.dashboard[0]) {
        strcpy(g_settings.dashboard, "-");
        for (int i = 0; i < g_choices.dash_n; i++) {
            if (!strncmp(g_choices.dash_ids[i], "pal", 3)) {
                strlcpy(g_settings.dashboard, g_choices.dash_ids[i], sizeof(g_settings.dashboard));
                break;
            }
        }
        settings_save();
    }
    cJSON *o = pal_get_json("/api/orbs", 10000);
    strcpy(g_choices.orb_opts, "Follow PAL\nTheme orb");
    strcpy(g_choices.orb_ids[0], "follow");
    strcpy(g_choices.orb_ids[1], "default");
    g_choices.orb_n = 2;
    cJSON_ArrayForEach(it, cJSON_GetObjectItem(o, "orbs")) {
        if (g_choices.orb_n >= 24) break;
        const char *n = cJSON_GetStringValue(cJSON_GetObjectItem(it, "name"));
        const char *id = cJSON_GetStringValue(cJSON_GetObjectItem(it, "id"));
        if (!n || !id) continue;
        size_t l = strlen(g_choices.orb_opts);
        snprintf(g_choices.orb_opts + l, sizeof(g_choices.orb_opts) - l, "\n%s", n);
        strlcpy(g_choices.orb_ids[g_choices.orb_n++], id, 64);
    }
    cJSON_Delete(o);
    if (pal_lock()) {
        ui_settings_refresh();
        pal_unlock();
    }
}

static void load_all(void)
{
    cJSON *info = pal_get_json("/api/device/info?model=" PAL_MODEL, 10000);
    if (info) {
        apply_info(info);
        cJSON_Delete(info);
    }
    load_choices();
    load_orb();
    load_pages();
    s_loaded = true;
}

// --- pairing --------------------------------------------------------------------------

static void request_code(void)
{
    pal_resp_t r;
    // pairing is LAN-only by design: always the LAN base
    if (pal_post_json("/api/pair/request", "{}", &r, 8000) == ESP_OK) pal_resp_free(&r);
}

static void start_pairing(void)
{
    s_pairing = true;
    ws_stop();
    request_code();
    if (pal_lock()) {
        ui_show_setup(NULL, NULL, NULL);
        ui_show_pairing(true, NULL);
        pal_unlock();
    }
}

static void redeem(const char *code)
{
    cJSON *b = cJSON_CreateObject();
    cJSON_AddStringToObject(b, "code", code);
    cJSON_AddStringToObject(b, "device_name", s_devname);
    char *body = cJSON_PrintUnformatted(b);
    cJSON_Delete(b);
    pal_resp_t r;
    g_settings.token[0] = 0;  // redeem is unauthenticated
    esp_err_t err = pal_post_json("/api/pair/redeem", body, &r, 10000);
    free(body);
    cJSON *j = err == ESP_OK && r.status == 200 ? cJSON_Parse(r.body) : NULL;
    if (err == ESP_OK) pal_resp_free(&r);
    const char *tok = cJSON_GetStringValue(cJSON_GetObjectItem(j, "token"));
    if (!tok) {
        cJSON_Delete(j);
        if (pal_lock()) {
            ui_show_pairing(true, "That code didn't work — here's a new one");
            pal_unlock();
        }
        request_code();
        return;
    }
    strlcpy(g_settings.token, tok, sizeof(g_settings.token));
    const char *gw = cJSON_GetStringValue(cJSON_GetObjectItem(j, "gateway_url"));
    strlcpy(g_settings.gateway, gw ? gw : "", sizeof(g_settings.gateway));
    cJSON_Delete(j);
    settings_save();
    s_pairing = false;
    if (pal_lock()) {
        ui_show_pairing(false, NULL);
        ui_toast("Paired");
        pal_unlock();
    }
    audio_beep(1);
    pal_choose_base();
    load_all();
    ws_start();
}

// --- connecting -------------------------------------------------------------------------

static void on_wifi_up(void)
{
    if (!g_settings.server[0]) {
        setup_screen("Looking for PAL…", "Searching your network", NULL);
        char base[128];
        bool found = false;
        for (int i = 0; i < 3 && !found; i++) found = net_discover_pal(base, sizeof(base));
        if (!found) {
            setup_screen("PAL not found",
                         "Couldn't find PAL on this network. Open Settings → Wi-Fi setup to enter its address, "
                         "or check that the PAL server is running.", NULL);
            return;
        }
        strlcpy(g_settings.server, base, sizeof(g_settings.server));
        settings_save();
    }
    pal_choose_base();
    if (!g_settings.token[0]) {
        start_pairing();
        return;
    }
    setup_screen(NULL, NULL, NULL);
    load_all();
    ws_start();
}

// --- the conversation turn -----------------------------------------------------------------

static void begin_listening(void)
{
    if (!g_settings.token[0] || s_pairing) return;
    if (pal_lock()) {
        ui_screen_wake();
        ui_show_home();
        pal_unlock();
    }
    audio_stop_playback();
    audio_beep(0);
    vTaskDelay(pdMS_TO_TICKS(160));   // let the earcon finish before the mic opens
    if (audio_record_start(true)) set_state(PAL_LISTENING);
}

static void finish_listening(void)
{
    size_t n;
    int16_t *pcm = audio_recording_data(&n);
    if (n < PAL_SAMPLE_RATE * 4 / 10) {
        set_state(PAL_IDLE);
        audio_beep(2);
        return;
    }
    set_state(PAL_PROCESSING);
    float *f = heap_caps_malloc(n * sizeof(float), MALLOC_CAP_SPIRAM);
    if (!f) { set_state(PAL_IDLE); return; }
    for (size_t i = 0; i < n; i++) f[i] = pcm[i] / 32768.0f;
    pal_resp_t r;
    char *text = NULL;
    if (pal_post_bin("/api/satellite/stt", f, n * sizeof(float), "application/octet-stream", "X-Sample-Rate",
                     "16000", &r, 30000) == ESP_OK) {
        cJSON *j = r.status == 200 ? cJSON_Parse(r.body) : NULL;
        const char *t = cJSON_GetStringValue(cJSON_GetObjectItem(j, "text"));
        if (t && t[0]) text = strdup(t);
        cJSON_Delete(j);
        pal_resp_free(&r);
    }
    free(f);
    if (!text) {
        set_state(PAL_IDLE);
        audio_beep(2);
        if (pal_lock()) {
            ui_set_transcript("Didn't catch that");
            pal_unlock();
        }
        return;
    }
    ESP_LOGI(TAG, "heard: %s", text);
    if (pal_lock()) {
        ui_set_transcript(text);
        pal_unlock();
    }
    cJSON *b = cJSON_CreateObject();
    cJSON_AddStringToObject(b, "text", text);
    char *body = cJSON_PrintUnformatted(b);
    cJSON_Delete(b);
    free(text);
    if (pal_post_json("/api/command", body, &r, 15000) == ESP_OK) {
        if (r.status != 200) {
            set_state(PAL_IDLE);
            toast("PAL didn't accept that");
        }
        pal_resp_free(&r);
    } else {
        set_state(PAL_IDLE);
        toast("Can't reach PAL");
    }
    free(body);
}

static void play_tts(void)
{
    pal_resp_t r;
    if (pal_get("/api/satellite/tts?rate=16000", &r, 20000) != ESP_OK) return;
    if (r.status == 200 && r.len > 44) {
        if (pal_lock()) {
            ui_screen_wake();
            pal_unlock();
        }
        set_state(PAL_SPEAKING);
        audio_play_wav((uint8_t *)r.body, r.len);   // takes ownership
    } else {
        pal_resp_free(&r);
    }
}

// --- socket messages ----------------------------------------------------------------------

static pal_state_t parse_state(const char *s)
{
    if (!s) return PAL_IDLE;
    if (!strcmp(s, "listening")) return PAL_LISTENING;
    if (!strcmp(s, "processing")) return PAL_PROCESSING;
    if (!strcmp(s, "speaking")) return PAL_SPEAKING;
    return PAL_IDLE;
}

static void on_msg(cJSON *m)
{
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(m, "type"));
    if (!type) return;
    if (!strcmp(type, "ha_states") || !strcmp(type, "ha_state")) {
        if (pal_lock()) {
            ha_on_states(cJSON_GetObjectItem(m, "states"), !strcmp(type, "ha_states"));
            pal_unlock();
        }
    } else if (!strcmp(type, "state")) {
        pal_state_t st = parse_state(cJSON_GetStringValue(cJSON_GetObjectItem(m, "state")));
        // our own audio decides when speaking ends; ignore the server's early idle
        if (st == PAL_IDLE && audio_playing()) return;
        if (audio_recording()) return;   // we're listening locally
        set_state(st);
    } else if (!strcmp(type, "response")) {
        if (pal_lock()) {
            ui_screen_wake();
            ui_set_response(cJSON_GetStringValue(cJSON_GetObjectItem(m, "text")));
            pal_unlock();
        }
    } else if (!strcmp(type, "transcription")) {
        cJSON *partial = cJSON_GetObjectItem(m, "is_partial");
        const char *spk = cJSON_GetStringValue(cJSON_GetObjectItem(m, "speaker"));
        if (!cJSON_IsTrue(partial) && (!spk || strcmp(spk, "ai")) && pal_lock()) {
            ui_set_transcript(cJSON_GetStringValue(cJSON_GetObjectItem(m, "text")));
            pal_unlock();
        }
    } else if (!strcmp(type, "tts_play")) {
        play_tts();
    } else if (!strcmp(type, "set_orb")) {
        if (!strcmp(g_settings.orb, "follow")) load_orb();
    } else if (!strcmp(type, "set_theme") || !strcmp(type, "themes_changed")) {
        cJSON *info = pal_get_json("/api/device/info?model=" PAL_MODEL, 10000);
        if (info) {
            apply_info(info);
            cJSON_Delete(info);
        }
        load_orb();
    } else if (!strcmp(type, "ble_pair")) {   // a phone asked through PAL (POST /api/satellite/ble_pair)
        ble_pair_begin(cJSON_GetStringValue(cJSON_GetObjectItem(m, "from")));
    } else if (!strcmp(type, "weather_update")) {
        cJSON *t = cJSON_GetObjectItem(m, "temp");
        if (pal_lock()) {
            ui_set_weather(cJSON_GetStringValue(cJSON_GetObjectItem(m, "condition")),
                           cJSON_IsNumber(t) ? t->valuedouble : NAN,
                           cJSON_GetStringValue(cJSON_GetObjectItem(m, "unit")),
                           cJSON_IsTrue(cJSON_GetObjectItem(m, "show")) && cJSON_IsNumber(t));
            pal_unlock();
        }
    } else if (!strcmp(type, "timer_countdown")) {
        cJSON *end = cJSON_GetObjectItem(m, "ends_at_epoch_ms");
        if (cJSON_IsNumber(end) && pal_lock()) {
            ui_screen_wake();
            ui_timer_countdown(cJSON_GetStringValue(cJSON_GetObjectItem(m, "timer_id")), (int64_t)end->valuedouble);
            pal_unlock();
        }
    } else if (!strcmp(type, "timer_countdown_cancel") || !strcmp(type, "timer_countdown_dismiss")) {
        if (pal_lock()) {
            ui_timer_cancel(cJSON_GetStringValue(cJSON_GetObjectItem(m, "timer_id")));
            pal_unlock();
        }
    }
}

// --- actions ------------------------------------------------------------------------------

static void ota_progress(int pct)
{
    if (pal_lock()) {
        ui_ota_progress(pct);
        pal_unlock();
    }
}

// --- Bluetooth link (BLE_LINK.md) ----------------------------------------------------------

static void ble_pair_begin(const char *from)
{
    if (!link_pair_start()) {
        toast("Bluetooth unavailable");
        return;
    }
    if (pal_lock()) {
        ui_screen_wake();
        pal_unlock();
    }
    char body[160];
    snprintf(body, sizeof(body), "%s%s%sOpen the PAL app → Settings → PAL Round → Pair. Press a button to cancel.",
             from ? "Requested by " : "", from ? from : "", from ? ".\n" : "");
    setup_screen("Pair a phone", body, NULL);
}

static void on_link(const char *ev)
{
    if (!strcmp(ev, "ble")) {
        net_enter_ble();
    } else if (ev[0] == 'c') {   // passkey to show
        char code[8];
        snprintf(code, sizeof(code), "%.3s %.3s", ev + 1, ev + 4);
        setup_screen(code, "Type this code on your phone", NULL);
    } else if (!strcmp(ev, "paired")) {
        g_settings.ble_bond = 1;
        settings_save();
        link_pair_finish();
        setup_screen(NULL, NULL, NULL);
        toast("Phone paired");
        audio_beep(1);
    } else if (!strcmp(ev, "pairend") || !strcmp(ev, "pairfail")) {
        link_pair_finish();
        setup_screen(NULL, NULL, NULL);
        toast(ev[4] == 'f' ? "Pairing failed" : "Pairing timed out");
    }
}

static bool ble_pair_cancel(void)
{
    if (!link_pairing()) return false;
    link_pair_finish();
    setup_screen(NULL, NULL, NULL);
    toast("Pairing cancelled");
    return true;
}

static void do_action(const char *a)
{
    if (!strcmp(a, "blepair")) {
        ble_pair_begin(NULL);
    } else if (!strcmp(a, "bleoff")) {
        net_leave_ble(0);
    } else if (!strcmp(a, "bleforget")) {
        if (net_ble_mode()) net_leave_ble(0);
        link_forget();
        toast("Phone forgotten");
    } else if (!strcmp(a, "netcheck")) {
        net_check_main();
    } else if (!strcmp(a, "wake")) {
        if (pal_lock()) {
            ui_screen_wake();
            pal_unlock();
        }
    } else if (!strcmp(a, "restart")) {
        esp_restart();
    } else if (!strcmp(a, "save_reboot")) {
        settings_save();
        vTaskDelay(pdMS_TO_TICKS(200));
        esp_restart();
    } else if (!strcmp(a, "reset")) {
        settings_factory_reset();
        esp_restart();
    } else if (!strcmp(a, "repair")) {
        g_settings.token[0] = 0;
        settings_save();
        start_pairing();
    } else if (!strcmp(a, "portal")) {
        ws_stop();
        portal_start();
        char qr[64], body[200];
        snprintf(qr, sizeof(qr), "WIFI:S:%s;T:nopass;;", portal_ssid());
        snprintf(body, sizeof(body), "Join \"%s\" with your phone, then open 192.168.4.1", portal_ssid());
        setup_screen("Wi-Fi setup", body, qr);
    } else if (!strcmp(a, "ota")) {
        cJSON *info = pal_get_json("/api/device/info?model=" PAL_MODEL, 10000);
        if (info) {
            apply_info(info);
            cJSON_Delete(info);
        }
        if (!g_choices.update[0]) {
            toast("Up to date");
        } else {
            ws_stop();
            ota_progress(0);
            if (pal_ota_update("/api/device/firmware/" PAL_MODEL ".bin", ota_progress) == ESP_OK) {
                esp_restart();
            }
            ota_progress(-1);
            toast("Update failed");
            ws_start();
        }
        if (pal_lock()) {
            ui_settings_refresh();
            pal_unlock();
        }
    }
}

// --- buttons -------------------------------------------------------------------------------

// BOOT is polled (40 ms) instead of interrupt-driven: GPIO edge interrupts are
// lost while the chip light-sleeps, and a poll wakes it anyway.
static void buttons_task(void *arg)
{
    int prev = 1, stable = 1;
    while (true) {
        int lvl = gpio_get_level(BOOT_BTN);
        if (lvl == prev && lvl != stable) {   // two equal samples = debounced
            stable = lvl;
            if (!g_settings.btn_lock) app_post(lvl == 0 ? EV_PTT_PRESS : EV_PTT_RELEASE);
            else if (lvl == 0) app_post(EV_BTN_LOCKED);
        }
        prev = lvl;
        vTaskDelay(pdMS_TO_TICKS(40));
    }
}

static void buttons_init(void)
{
    gpio_config_t c = {
        .pin_bit_mask = 1ULL << BOOT_BTN, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&c);
    xTaskCreateWithCaps(buttons_task, "btn", 2560, NULL, 6, NULL, MALLOC_CAP_SPIRAM);
}

static void tick_cb(void *arg) { app_post(EV_TICK); }

// --- app task --------------------------------------------------------------------------------

// --- voice notes -------------------------------------------------------------------------
//
// Double-press a button (BOOT, PWR or the orb): the turn that the first press
// started is dropped and a voice note starts recording. The audio streams to
// POST /api/satellite/note (chunked, 16 kHz s16le) while you talk, so notes can
// be long (10 min cap) without buffering them. Press again to finish: the
// server transcribes it and stores it in the Notes list.

#define DOUBLE_PRESS_US 450000
static StreamBufferHandle_t s_note_sb;
static TaskHandle_t s_note_task;
static volatile bool s_note_on;            // recording or uploading
static bool s_turn_aborted;
static int64_t s_note_t0, s_last_press_us;
static int s_note_status;                  // HTTP status, -1 = no answer
static bool s_note_stored;
static char *s_note_text;

static int note_next(char *buf, size_t max)
{
    size_t n = xStreamBufferReceive(s_note_sb, buf, max, pdMS_TO_TICKS(200));
    if (n) return (int)n;
    if (audio_recording() || xStreamBufferBytesAvailable(s_note_sb)) return 0;
    return -1;   // recording over and everything sent
}

static void note_task(void *arg)
{
    while (true) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        pal_resp_t r;
        esp_err_t err = pal_post_stream("/api/satellite/note", "application/octet-stream", "X-Sample-Rate", "16000",
                                        "X-Sample-Format", "s16le", note_next, &r, 180000);
        if (audio_recording()) audio_record_stop();      // the upload died early: stop the mic too
        while (audio_recording()) vTaskDelay(pdMS_TO_TICKS(20));
        free(s_note_text);
        s_note_text = NULL;
        s_note_stored = false;
        s_note_status = err == ESP_OK ? r.status : -1;
        if (err == ESP_OK) {
            cJSON *j = cJSON_Parse(r.body);
            const char *t = cJSON_GetStringValue(cJSON_GetObjectItem(j, "text"));
            if (t && t[0]) s_note_text = strdup(t);
            s_note_stored = !cJSON_IsFalse(cJSON_GetObjectItem(j, "stored")) &&
                            cJSON_IsNumber(cJSON_GetObjectItem(j, "id"));
            cJSON_Delete(j);
            pal_resp_free(&r);
        }
        xStreamBufferReset(s_note_sb);
        s_note_on = false;
        app_post(EV_NOTE_DONE);
    }
}

static void note_show_elapsed(void)
{
    int s = (int)((esp_timer_get_time() - s_note_t0) / 1000000);
    char l1[48];
    snprintf(l1, sizeof(l1), "\xE2\x97\x8F Recording note  %d:%02d", s / 60, s % 60);
    if (pal_lock()) {
        ui_note_status(l1, "Press again to save");
        pal_unlock();
    }
}

static void note_begin(void)
{
    if (s_note_on || !g_settings.token[0] || s_pairing) return;
    if (!s_note_sb) s_note_sb = xStreamBufferCreateWithCaps(256 * 1024, 1, MALLOC_CAP_SPIRAM);  // ~8 s of slack
    if (!s_note_task) xTaskCreateWithCaps(note_task, "note", 6144, NULL, 5, &s_note_task, MALLOC_CAP_SPIRAM);
    if (!s_note_sb || !s_note_task) return;
    // drop the turn the first press started
    // (its EV_REC_DONE is pending or about to be: swallow it)
    if (audio_recording() || s_state == PAL_LISTENING) s_turn_aborted = true;
    if (audio_recording()) audio_record_abort();
    while (audio_recording()) vTaskDelay(pdMS_TO_TICKS(10));
    if (pal_lock()) {
        ui_screen_wake();
        ui_show_home();
        pal_unlock();
    }
    audio_beep(0);
    audio_beep(0);                 // two blips = note (one = talk)
    vTaskDelay(pdMS_TO_TICKS(320));
    xStreamBufferReset(s_note_sb);
    if (!audio_note_start(s_note_sb)) {
        set_state(PAL_IDLE);
        return;
    }
    s_note_on = true;
    s_note_t0 = esp_timer_get_time();
    set_state(PAL_LISTENING);
    note_show_elapsed();
    xTaskNotifyGive(s_note_task);
    ESP_LOGI(TAG, "voice note: recording");
}

static void note_end(void)
{
    audio_record_stop();
    set_state(PAL_PROCESSING);
    if (pal_lock()) {
        ui_note_status("Saving note\xE2\x80\xA6", NULL);
        pal_unlock();
    }
}

static void note_done(void)
{
    set_state(PAL_IDLE);
    const char *l1, *l2 = NULL;
    if (s_note_status == 200 && s_note_text) {
        l1 = s_note_stored ? "Note saved" : "Transcribed, but not saved";
        l2 = s_note_text;
        audio_beep(1);
    } else if (s_note_status == 422) {
        l1 = "Nothing heard \xE2\x80\x94 no note saved";
        audio_beep(2);
    } else {
        l1 = "Couldn't save the note";
        l2 = s_note_status > 0 ? "PAL refused it" : "PAL isn't reachable";
        audio_beep(2);
    }
    ESP_LOGI(TAG, "voice note: status %d stored %d", s_note_status, s_note_stored);
    if (pal_lock()) {
        ui_note_status(l1, l2);
        pal_unlock();
    }
}

// Every "talk" press (BOOT, PWR, orb tap) comes through here.
static void talk_press(void)
{
    int64_t now = esp_timer_get_time();
    bool dbl = now - s_last_press_us < DOUBLE_PRESS_US;
    s_last_press_us = now;
    if (s_note_on) {               // recording → finish; uploading → ignore
        if (audio_recording()) note_end();
        return;
    }
    if (dbl && (audio_recording() || s_state == PAL_LISTENING)) {
        s_last_press_us = 0;
        note_begin();
        return;
    }
    if (audio_recording()) audio_record_stop();
    else begin_listening();
}

static void app_task(void *arg)
{
    app_event_t e;
    while (true) {
        if (xQueueReceive(s_q, &e, portMAX_DELAY) != pdTRUE) continue;
        switch (e.type) {
        case EV_WIFI_UP:
            if (!portal_active()) on_wifi_up();
            break;
        case EV_WIFI_DOWN:
            ws_stop();
            break;
        case EV_WS_UP:
            if (!s_loaded) load_all();
            ha_resend_retain();
            esp_ota_mark_app_valid_cancel_rollback();
            break;
        case EV_WS_DOWN:
            break;
        case EV_WS_MSG:
            on_msg(e.json);
            cJSON_Delete(e.json);
            break;
        case EV_PTT_PRESS:
            if (ble_pair_cancel()) break;
            s_ptt_down_us = esp_timer_get_time();
            talk_press();
            break;
        case EV_PTT_RELEASE:
            // hold-to-talk on the BOOT button: a long hold ends on release
            if (!s_note_on && audio_recording() && esp_timer_get_time() - s_ptt_down_us > 700000)
                audio_record_stop();
            break;
        case EV_REC_DONE:
            if (s_turn_aborted) {          // the turn became a voice note (double press)
                s_turn_aborted = false;
                break;
            }
            audio_beep(1);
            finish_listening();
            break;
        case EV_LINK:
            on_link(e.str);
            break;
        case EV_NOTE_DONE:
            note_done();
            break;
        case EV_PLAY_DONE:
            if (!audio_playing() && s_state == PAL_SPEAKING) set_state(PAL_IDLE);
            break;
        case EV_BTN_LOCKED:
            // only a hint when someone is looking; a pocket press stays silent and dark
            if (ui_screen_on()) toast("Locked - unlock on the page left of the orb");
            break;
        case EV_PWR_SHORT: {
            if (g_settings.btn_lock) {
                if (ui_screen_on()) toast("Locked - unlock on the page left of the orb");
                break;
            }
            if (ble_pair_cancel()) break;
            // either button talks; a press while the screen is off just wakes it
            bool was_on = true;
            if (pal_lock()) {
                was_on = ui_screen_on();
                ui_screen_wake();
                pal_unlock();
            }
            if (!was_on) break;
            talk_press();
            break;
        }
        case EV_PWR_LONG:
            if (g_settings.btn_lock) {
                if (ui_screen_on()) toast("Locked - unlock on the page left of the orb");
                break;
            }
            setup_screen("Goodbye", "Press the power button to turn PAL Round back on.", NULL);
            vTaskDelay(pdMS_TO_TICKS(1200));
            pmu_power_off();
            break;
        case EV_PAIR_CODE:
            redeem(e.str);
            break;
        case EV_SETTINGS_CHANGED:
            settings_save();
            break;
        case EV_RELOAD:
            if (g_settings.token[0]) {
                load_orb();
                load_pages();
            }
            break;
        case EV_ACTION:
            do_action(e.str);
            break;
        case EV_TICK:
            s_ticks++;
            if (s_note_on && audio_recording()) note_show_elapsed();
            if (s_ticks % 600 == 0) net_check_main();   // on the backup network: is the main one back?
            if (pal_lock()) {
                ui_tick();
                pal_unlock();
            }
            if (s_ticks % 25 == 0) {
                char ping[64];
                snprintf(ping, sizeof(ping), "{\"type\":\"ping\",\"battery\":%d,\"charging\":%s}", pmu_battery_pct(),
                         pmu_charging() ? "true" : "false");
                ws_send_text(ping);
            }
            if (s_ticks % 10 == 0)
                ESP_LOGI(TAG, "heap: internal %u (largest dma %u)", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                         (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA));
            break;
        }
    }
}

static void storage_init(void)
{
    esp_vfs_fat_mount_config_t mc = {.max_files = 16, .format_if_mount_failed = true, .allocation_unit_size = 16384};
    wl_handle_t wl;
    if (esp_vfs_fat_spiflash_mount_rw_wl("/orb", "storage", &mc, &wl) != ESP_OK) {
        ESP_LOGE(TAG, "storage mount failed (orb clips won't be cached)");
    }
}

void app_main(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_devname, sizeof(s_devname), "PAL Round %02X%02X", mac[4], mac[5]);
    ESP_LOGI(TAG, "%s firmware %s", s_devname, pal_fw_version());

    // Dynamic frequency scaling + automatic light sleep: the CPU runs at 240 MHz
    // only while a task is busy, drops to 40 MHz otherwise, and light-sleeps when
    // every task is waiting (Wi-Fi stays associated through modem sleep). While a
    // USB host is attached, light sleep is held off so the serial console works.
    esp_pm_config_t pm = {.max_freq_mhz = 240, .min_freq_mhz = 40, .light_sleep_enable = true};
    esp_err_t pm_err = esp_pm_configure(&pm);
    if (pm_err != ESP_OK) ESP_LOGW(TAG, "power management: %s", esp_err_to_name(pm_err));

    s_q = xQueueCreate(32, sizeof(app_event_t));
    settings_load();
    storage_init();

    bsp_display_start();
    fonts_init();
    if (pal_lock()) {
        bsp_display_brightness_set(g_settings.brightness);   // under the lock: not mid-flush
        ui_init();
        ui_settings_refresh();
        pal_unlock();
    }
    pmu_init();
    audio_init();
    buttons_init();
    console_start();
    xTaskCreatePinnedToCore(app_task, "app", 9216, NULL, 5, NULL, 1);  // internal: it writes flash

    net_start();
    if (!g_settings.wifi_ssid[0]) {
        app_post_str(EV_ACTION, "portal");
    } else {
        setup_screen("Connecting…", g_settings.wifi_ssid, NULL);
    }
    const esp_timer_create_args_t ta = {.callback = tick_cb, .name = "tick"};
    esp_timer_handle_t t;
    esp_timer_create(&ta, &t);
    esp_timer_start_periodic(t, 1000000);
    ESP_LOGI(TAG, "heap: internal free %u (largest %u), psram free %u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}
