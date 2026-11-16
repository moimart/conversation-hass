// PAL Round — a native PAL satellite for the Waveshare ESP32-S3-Touch-AMOLED-1.75C.
//
// Shared declarations for every module. The firmware is event driven: network
// callbacks, buttons and the UI post `app_event_t`s to one app task (main.c),
// which owns the conversation state machine and calls into the UI under the
// display lock.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "cJSON.h"
#include "lvgl.h"

#define PAL_MODEL "pal-round"
#define PAL_SAMPLE_RATE 16000

// ---------------------------------------------------------------------------
// settings (NVS)

typedef struct {
    char wifi_ssid[33];
    char wifi_pass[65];
    char wifi2_ssid[33];   // backup network, used when the main one is unreachable ("" = none)
    char wifi2_pass[65];
    char server[128];      // LAN base, e.g. http://10.20.30.185:8765
    char gateway[128];     // remote base handed out at pairing (may be "")
    char token[96];        // pairing token ("" = not paired)
    char dashboard[64];    // HA dashboard url_path for the pages ("-" = none)
    char orb[64];          // "follow" or an orb id
    uint8_t volume;        // 0..100
    uint8_t brightness;    // 5..100
    uint16_t screen_timeout_s; // 0 = never
    uint8_t ble_bond;      // a phone is bonded for the Bluetooth link (BLE_LINK.md)
} pal_settings_t;

extern pal_settings_t g_settings;
void settings_load(void);
void settings_save(void);
void settings_factory_reset(void);

// ---------------------------------------------------------------------------
// app events

typedef enum {
    EV_WIFI_UP,
    EV_WIFI_DOWN,
    EV_WS_UP,
    EV_WS_DOWN,
    EV_WS_MSG,          // json: owned cJSON (freed by the app task)
    EV_PTT_PRESS,       // orb tap / BOOT button down
    EV_PTT_RELEASE,     // BOOT button up (hold-to-talk)
    EV_REC_DONE,        // recording finished (VAD or release)
    EV_PLAY_DONE,
    EV_PWR_SHORT,
    EV_PWR_LONG,
    EV_PAIR_CODE,       // str: 6 digits typed on the keypad
    EV_SETTINGS_CHANGED,
    EV_RELOAD,          // re-fetch info / orb / pages
    EV_TICK,            // 1 Hz housekeeping
    EV_ACTION,          // str: "repair" | "portal" | "ota" | "restart" | "reset"
    EV_NOTE_DONE,       // voice note upload finished (result via note_result())
    EV_LINK,            // str: "ble" (Wi-Fi gave up: go relay) | "c<passkey>" | "paired" | "pairend" | "pairfail"
} app_event_type_t;

typedef struct {
    app_event_type_t type;
    cJSON *json;
    char str[16];
} app_event_t;

void app_post(app_event_type_t type);
void app_post_json(cJSON *json);
void app_post_str(app_event_type_t type, const char *s);

// ---------------------------------------------------------------------------
// conversation state shown by the orb

typedef enum { PAL_IDLE, PAL_LISTENING, PAL_PROCESSING, PAL_SPEAKING } pal_state_t;

// ---------------------------------------------------------------------------
// net.c — Wi-Fi, SNTP, mDNS discovery, setup portal

void net_start(void);
bool net_is_up(void);
void net_power_save(bool deep);                  // deeper Wi-Fi modem sleep (screen off)
const char *net_ssid(void);                      // the network in use (main or backup)
bool net_on_backup(void);
void net_force(int idx);                         // 0 = main, 1 = backup (tests)
void net_check_main(void);                      // on backup: switch back if the main network is in range
int net_rssi(void);
bool net_discover_pal(char *base, size_t len);   // _pal._tcp → http://ip:port
void net_set_tz(const char *posix_tz);
bool net_time_valid(void);
bool net_ble_mode(void);                         // Wi-Fi off, reaching PAL through the phone
void net_enter_ble(void);
void net_leave_ble(int idx);                     // back to Wi-Fi network idx (0 main, 1 backup)

// ---------------------------------------------------------------------------
// ble_link.c — PAL through a bonded phone over BLE (firmware/pal-round/BLE_LINK.md)

bool link_start(void);                           // relay mode: advertise for the bonded phone
void link_stop(void);
bool link_pair_start(void);                      // 2-minute pairing window (shows a passkey)
void link_pair_finish(void);                     // window over: drop the stack unless relaying
void link_forget(void);                          // delete bonds
bool link_active(void);                          // relay mode on
bool link_up(void);                              // phone connected + HELLO exchanged
bool link_pairing(void);
int link_streams(void);
const char *link_state(void);
void portal_start(void);                         // SoftAP + captive setup page
bool portal_active(void);
const char *portal_ssid(void);

// ---------------------------------------------------------------------------
// pal_client.c — HTTP helpers against the active base

const char *pal_base(void);                      // active base (LAN or gateway)
void pal_choose_base(void);                      // probe LAN /health, else gateway
typedef struct { int status; char *body; size_t len; } pal_resp_t;  // body is NUL-terminated, PSRAM
esp_err_t pal_get(const char *path, pal_resp_t *out, int timeout_ms);
esp_err_t pal_post_json(const char *path, const char *json, pal_resp_t *out, int timeout_ms);
esp_err_t pal_post_bin(const char *path, const void *data, size_t len, const char *ctype,
                       const char *extra_hdr_name, const char *extra_hdr_val, pal_resp_t *out, int timeout_ms);
// POST a body of unknown length with chunked transfer encoding. `next` fills buf
// and returns >0 bytes, 0 = nothing yet (call again), <0 = end of body.
esp_err_t pal_post_stream(const char *path, const char *ctype, const char *h1, const char *v1, const char *h2,
                          const char *v2, int (*next)(char *buf, size_t max), pal_resp_t *out, int timeout_ms);
esp_err_t pal_get_to_file(const char *path, const char *file, int timeout_ms);
void pal_resp_free(pal_resp_t *r);
cJSON *pal_get_json(const char *path, int timeout_ms);
esp_err_t pal_call_service(const char *domain, const char *service, const char *entity, cJSON *data);
esp_err_t pal_ota_update(const char *path, void (*progress)(int pct));

// ---------------------------------------------------------------------------
// pal_ws.c — the /ws/ui satellite socket

void ws_start(void);
void ws_stop(void);
bool ws_connected(void);
void ws_send_text(const char *text);
void ws_send_retain(const char *const *ids, int n);

// ---------------------------------------------------------------------------
// audio.c — ES8311 speaker, ES7210 mic

void audio_init(void);
void audio_set_volume(int vol);
bool audio_record_start(bool vad);               // records into PSRAM; EV_REC_DONE when done
void audio_record_stop(void);
void audio_record_abort(void);                   // stop and discard (EV_REC_DONE still fires)
bool audio_note_start(void *stream_buffer);      // voice note: s16le frames into a StreamBuffer, no VAD, ≤10 min
bool audio_recording(void);
int16_t *audio_recording_data(size_t *samples);  // valid until the next record
float audio_level(void);                         // 0..1, mic or speaker, for the orb
bool audio_play_wav(uint8_t *wav, size_t len);   // takes ownership (free()), async; EV_PLAY_DONE
void audio_stop_playback(void);
bool audio_playing(void);
void audio_beep(int kind);                       // 0 = listen, 1 = done, 2 = error

// ---------------------------------------------------------------------------
// pmu.c — AXP2101 (battery + power key)

void pmu_init(void);
int pmu_battery_pct(void);     // -1 = no battery
bool pmu_charging(void);
bool pmu_vbus(void);
void pmu_power_off(void);

// ---------------------------------------------------------------------------
// fonts.c — FreeType fonts from the embedded TTFs

typedef struct {
    const lv_font_t *text16, *text20, *text26, *text40, *clock;
    const lv_font_t *icon22, *icon34, *icon56;
} pal_fonts_t;
extern pal_fonts_t g_fonts;
void fonts_init(void);
// UTF-8 for an MDI codepoint (buf >= 5)
const char *mdi(uint32_t cp, char *buf);

#include "icons.h"   // ICON_* codepoints (generated)

// ---------------------------------------------------------------------------
// theme colours (from /api/device/info)

typedef struct { lv_color_t bg, text, text_dim, accent, accent_deep; } pal_theme_t;
extern pal_theme_t g_theme;

// ---------------------------------------------------------------------------
// orb.c — clip player (.palm) or native orb

void orb_create(lv_obj_t *parent, int size);
void orb_set_state(pal_state_t st);
void orb_set_active(bool on);                     // false = stop animating (screen off)
void orb_load_manifest(cJSON *manifest);          // downloads missing clips, switches
void orb_native(void);                            // drop clips, draw the native orb
void orb_apply_theme(void);
lv_obj_t *orb_obj(void);

// ---------------------------------------------------------------------------
// ha.c — HA pages + entity states

void ha_build_pages(cJSON *pages);                // from /api/device/pages
void ha_on_states(cJSON *states, bool full);      // ha_states / ha_state
void ha_resend_retain(void);
int ha_page_count(void);

// ---------------------------------------------------------------------------
// ui.c

void ui_init(void);
lv_obj_t *ui_add_page(void);                      // a new HA tile right of home
void ui_clear_pages(void);
void ui_show_setup(const char *title, const char *body, const char *qr);  // full-screen message
void ui_show_pairing(bool show, const char *err);
void ui_show_home(void);
void ui_set_state(pal_state_t st);
void ui_set_transcript(const char *text);
void ui_set_response(const char *text);
int ui_orb_overlays(lv_area_t *out, int max);  // -1: orb not directly blittable; else rects drawn over it
void ui_note_status(const char *line1, const char *line2);  // voice note banner in the text box
void ui_set_weather(const char *cond, double temp, const char *unit, bool show);
void ui_set_status(void);                          // battery / wifi icons
void ui_tick(void);                                // clock + timers, 1 Hz
void ui_timer_countdown(const char *id, int64_t ends_at_ms);
void ui_timer_cancel(const char *id);
void ui_apply_theme(void);
void ui_screen_wake(void);
void ui_screen_sleep(void);
bool ui_screen_on(void);
void ui_activity(void);                            // reset the screen timeout
void ui_settings_refresh(void);
void ui_settings_open(void);
void ui_no_swipe(lv_obj_t *obj);
void ui_settings_close(void);
void ui_toast(const char *text);
void ui_ota_progress(int pct);                     // -1 = hide
void ui_set_pair_message(const char *msg);

// settings sheet data, filled by main.c (dropdown options are "\n"-joined)
typedef struct {
    char dash_opts[512];          // titles
    char dash_ids[16][64];        // matching url_paths ("" = default)
    int dash_n;
    char orb_opts[512];           // "Follow PAL\n<orb names>"
    char orb_ids[24][64];         // "follow", ids…
    int orb_n;
    char update[32];              // available firmware version or ""
} ui_choices_t;
extern ui_choices_t g_choices;
uint32_t ha_weather_icon(const char *condition);

// helpers
lv_color_t pal_color_hex(const char *hex, lv_color_t fallback);
bool pal_lock(void);
void pal_unlock(void);
const char *pal_device_name(void);
const char *pal_fw_version(void);
void *pal_psram_malloc(size_t n);
