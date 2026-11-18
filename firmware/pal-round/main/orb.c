// The orb. Two renderers:
//
//  * clips — the hub's orb (theme state_videos or a custom orb), re-rendered
//    by the server as .palm files (JPEG frames, circular-masked, 12 fps). They
//    are cached on the FAT partition (/orb/<key>.palm) and played by a task
//    that decodes each frame with esp_new_jpeg into one of two RGB565 buffers
//    and hands it to an lv_image. Single-frame clips (photo orbs) decode once.
//  * native — no media: a glowing accent-coloured orb drawn with LVGL that
//    breathes when idle, swells with the mic/speaker level and spins an arc
//    while thinking.
//
// A ring around either one shows the conversation state.
#include <dirent.h>
#include <math.h>
#include <string.h>
#include <sys/stat.h>
#include "app.h"
#include "bsp/esp-bsp.h"
#include "esp_lcd_panel_ops.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "esp_heap_caps.h"
#include "esp_jpeg_dec.h"
#include "src/misc/cache/instance/lv_image_cache.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "orb";

#define ORB_DIR "/orb"
#define MAX_FRAMES 256

typedef struct {
    char key[24];
    FILE *f;
    uint16_t fps, w, h, count;
    uint32_t base;
    uint32_t *offs;     // count+1
} clip_t;

static lv_obj_t *s_wrap, *s_img, *s_ring, *s_arc, *s_core, *s_glow;
static int s_size;
static clip_t s_clips[4];           // per state (may share a key)
static bool s_have_clips;
static volatile pal_state_t s_state = PAL_IDLE;
static volatile bool s_active = true;
static volatile bool s_reload;
static SemaphoreHandle_t s_mu;
static TaskHandle_t s_task;
static lv_image_dsc_t s_dsc[2];
static uint8_t *s_px[2];
static uint8_t *s_in;
static size_t s_in_cap;
static lv_timer_t *s_native_timer;

static void clip_close(clip_t *c)
{
    if (c->f) fclose(c->f);
    free(c->offs);
    memset(c, 0, sizeof(*c));
}

static bool clip_open(clip_t *c, const char *key)
{
    memset(c, 0, sizeof(*c));
    char path[64];
    snprintf(path, sizeof(path), ORB_DIR "/%s.palm", key);
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    uint8_t h[16];
    if (fread(h, 1, 16, f) != 16 || memcmp(h, "PALM", 4)) { fclose(f); return false; }
    c->fps = h[6] | h[7] << 8;
    c->w = h[8] | h[9] << 8;
    c->h = h[10] | h[11] << 8;
    c->count = h[12] | h[13] << 8;
    if (!c->count || c->count > MAX_FRAMES) { fclose(f); return false; }
    c->offs = malloc(4 * (c->count + 1));
    if (!c->offs || fread(c->offs, 4, c->count + 1, f) != (size_t)c->count + 1) {
        free(c->offs);
        fclose(f);
        return false;
    }
    c->base = 16 + 4 * (c->count + 1);
    c->f = f;
    strlcpy(c->key, key, sizeof(c->key));
    return true;
}

// Decode frame i of clip c into s_px[buf]. Caller holds s_mu.
static bool decode_frame(clip_t *c, int i, int buf)
{
    uint32_t len = c->offs[i + 1] - c->offs[i];
    if (len > s_in_cap) {
        free(s_in);
        s_in = heap_caps_malloc(len + 1024, MALLOC_CAP_SPIRAM);
        s_in_cap = s_in ? len + 1024 : 0;
        if (!s_in) return false;
    }
    if (fseek(c->f, c->base + c->offs[i], SEEK_SET) || fread(s_in, 1, len, c->f) != len) return false;
    jpeg_dec_config_t cfg = DEFAULT_JPEG_DEC_CONFIG();
    cfg.output_type = JPEG_PIXEL_FORMAT_RGB565_LE;
    jpeg_dec_handle_t dec = NULL;
    if (jpeg_dec_open(&cfg, &dec) != JPEG_ERR_OK) return false;
    jpeg_dec_io_t io = {.inbuf = s_in, .inbuf_len = (int)len};
    jpeg_dec_header_info_t info;
    bool ok = jpeg_dec_parse_header(dec, &io, &info) == JPEG_ERR_OK && info.width == s_size && info.height == s_size;
    if (ok) {
        io.outbuf = s_px[buf];
        ok = jpeg_dec_process(dec, &io) == JPEG_ERR_OK;
    }
    jpeg_dec_close(dec);
    return ok;
}

// Showing a frame. Through LVGL a 448 px frame costs ~100 ms of software
// rendering (it can't keep 12 fps), so when the orb is plainly on screen the
// frame goes straight to the panel in 8-row bands (byte-swapped into two small
// internal DMA buffers that alternate: each draw_bitmap first waits for the
// previous transfer). Bands under the text box / status / timer / toast are
// handed to LVGL instead (invalidated), which draws them with this same frame
// underneath. LVGL's image keeps pointing at the newest frame without being
// invalidated, so anything it redraws later matches what's on the glass.
#define BAND 8
static uint16_t *s_bounce[2];
static int s_bounce_i;
static bool s_try_direct;          // the orb was plainly on screen at the last LVGL-drawn frame

static bool blit_direct(int buf)
{
    lv_area_t holes[4];
    int nh = ui_orb_overlays(holes, 4);
    esp_lcd_panel_handle_t panel = bsp_display_panel();
    if (nh < 0 || !panel || lv_obj_has_flag(s_img, LV_OBJ_FLAG_HIDDEN)) return false;
    if (!s_bounce[0]) {
        for (int i = 0; i < 2; i++) s_bounce[i] = heap_caps_malloc(s_size * BAND * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_bounce[0] || !s_bounce[1]) return false;
    }
    lv_area_t img;
    lv_obj_get_coords(s_img, &img);
    // LVGL's copy of the image: point it at this frame, no invalidation
    const lv_image_dsc_t *src = lv_image_get_src(s_img);
    lv_image_dsc_t *d = (lv_image_dsc_t *)src;
    if (d != &s_dsc[0] && d != &s_dsc[1]) return false;
    d->data = s_px[buf];
    lv_image_cache_drop(d);
    for (int y = 0; y < s_size; y += BAND) {
        int h = s_size - y < BAND ? s_size - y : BAND;
        lv_area_t band = {img.x1, img.y1 + y, img.x1 + s_size - 1, img.y1 + y + h - 1};
        bool hidden = false;
        for (int i = 0; i < nh && !hidden; i++)
            hidden = band.x1 <= holes[i].x2 && holes[i].x1 <= band.x2 && band.y1 <= holes[i].y2 && holes[i].y1 <= band.y2;
        if (hidden) {
            lv_obj_invalidate_area(s_img, &band);
            continue;
        }
        uint16_t *b = s_bounce[s_bounce_i];
        s_bounce_i ^= 1;
        const uint16_t *p = (const uint16_t *)s_px[buf] + y * s_size;
        for (int i = 0; i < s_size * h; i++) b[i] = (uint16_t)(p[i] << 8 | p[i] >> 8);   // panel wants big-endian
        esp_lcd_panel_draw_bitmap(panel, band.x1, band.y1, band.x2 + 1, band.y2 + 1, b);
    }
    return true;
}

static void show_buf(int buf)
{
    if (!pal_lock()) return;
    if (s_try_direct && blit_direct(buf)) {
        pal_unlock();
        return;
    }
    // first frame of a clip, or the orb is under something: the LVGL way
    s_dsc[buf].data = s_px[buf];
    lv_image_cache_drop(&s_dsc[buf]);
    lv_image_set_src(s_img, &s_dsc[buf]);
    lv_obj_remove_flag(s_img, LV_OBJ_FLAG_HIDDEN);
    lv_area_t tmp[4];
    s_try_direct = ui_orb_overlays(tmp, 4) >= 0;
    pal_unlock();
}

static void player_task(void *arg)
{
    int frame = 0, back = 0;
    clip_t *cur = NULL;
    pal_state_t shown_state = PAL_IDLE;
    bool still_done = false;
    while (true) {
        if (!s_active || !s_have_clips) {
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);   // woken by set_active / a new manifest
            continue;
        }
        int64_t t0 = esp_timer_get_time();
        xSemaphoreTake(s_mu, portMAX_DELAY);
        if (s_reload) {
            s_reload = false;
            cur = NULL;
        }
        pal_state_t st = s_state;
        clip_t *want = &s_clips[st];
        if (!want->f) want = &s_clips[PAL_IDLE];
        if (want != cur || st != shown_state) {
            if (!cur || strcmp(want->key, cur->key)) { frame = 0; still_done = false; }
            cur = want;
            shown_state = st;
        }
        bool ok = false;
        int fps = cur && cur->fps ? cur->fps : 12;
        int64_t td = 0;
        if (cur && cur->f && !(cur->count == 1 && still_done)) {
            int64_t a = esp_timer_get_time();
            ok = decode_frame(cur, frame, back);
            td = esp_timer_get_time() - a;
            frame = (frame + 1) % cur->count;
            if (cur->count == 1) still_done = true;
        }
        xSemaphoreGive(s_mu);
        if (ok) {
            show_buf(back);
            back ^= 1;
            static int frames;
            static int64_t since, dsum;
            frames++;
            dsum += td;
            if (!since) since = t0;
            if (t0 - since >= 10000000) {
                ESP_LOGD(TAG, "orb %d frames/10s (clip fps %d, decode %lld ms, decode+show %lld ms)", frames, fps,
                         (long long)(dsum / frames / 1000), (long long)((esp_timer_get_time() - t0) / 1000));
                frames = 0;
                dsum = 0;
                since = t0;
            }
        }
        int64_t spent = (esp_timer_get_time() - t0) / 1000;
        int wait = 1000 / fps - (int)spent;
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(wait > 5 ? wait : 5));
    }
}

// --- native orb ------------------------------------------------------------------

static void native_tick(lv_timer_t *t)
{
    if (!s_core || lv_obj_has_flag(s_core, LV_OBJ_FLAG_HIDDEN) || !s_active) return;
    static float phase;
    phase += 0.06f;
    float lvl = audio_level();
    float breathe = 0.5f + 0.5f * sinf(phase * (s_state == PAL_IDLE ? 0.6f : 1.6f));
    float k;
    switch (s_state) {
    case PAL_LISTENING: k = 0.62f + 0.25f * lvl + 0.04f * breathe; break;
    case PAL_SPEAKING: k = 0.60f + 0.30f * lvl; break;
    case PAL_PROCESSING: k = 0.58f + 0.05f * breathe; break;
    default: k = 0.52f + 0.06f * breathe; break;
    }
    int d = (int)(s_size * k);
    lv_obj_set_size(s_core, d, d);
    lv_obj_set_style_shadow_width(s_core, (int)(s_size * (0.10f + 0.12f * (lvl + breathe * 0.3f))), 0);
    lv_obj_set_style_shadow_opa(s_core, (lv_opa_t)(150 + 80 * breathe), 0);
}

static void arc_spin(void *obj, int32_t v) { lv_arc_set_rotation(obj, v); }

static void apply_state_style(void)
{
    lv_color_t accent = g_theme.accent;
    lv_opa_t ring_opa = LV_OPA_30;
    int ring_w = 3;
    switch (s_state) {
    case PAL_LISTENING: ring_opa = LV_OPA_COVER; ring_w = 6; break;
    case PAL_PROCESSING: ring_opa = LV_OPA_50; ring_w = 4; break;
    case PAL_SPEAKING: ring_opa = LV_OPA_80; ring_w = 5; break;
    default: break;
    }
    lv_obj_set_style_border_color(s_ring, accent, 0);
    lv_obj_set_style_border_opa(s_ring, ring_opa, 0);
    lv_obj_set_style_border_width(s_ring, ring_w, 0);
    lv_obj_set_style_shadow_color(s_ring, accent, 0);
    lv_obj_set_style_shadow_opa(s_ring, s_state == PAL_IDLE ? LV_OPA_TRANSP : LV_OPA_60, 0);
    lv_obj_set_style_arc_color(s_arc, accent, LV_PART_INDICATOR);
    if (s_state == PAL_PROCESSING) lv_obj_remove_flag(s_arc, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_arc, LV_OBJ_FLAG_HIDDEN);
}

void orb_apply_theme(void)
{
    if (!s_wrap) return;
    lv_obj_set_style_bg_color(s_core, g_theme.accent_deep, 0);
    lv_obj_set_style_bg_grad_color(s_core, g_theme.accent, 0);
    lv_obj_set_style_shadow_color(s_core, g_theme.accent, 0);
    lv_obj_set_style_bg_color(s_glow, g_theme.accent_deep, 0);
    apply_state_style();
}

void orb_create(lv_obj_t *parent, int size)
{
    s_mu = xSemaphoreCreateMutex();
    s_size = size;
    for (int i = 0; i < 2; i++) {
        s_px[i] = heap_caps_aligned_alloc(16, size * size * 2, MALLOC_CAP_SPIRAM);
        memset(s_px[i], 0, size * size * 2);
        s_dsc[i] = (lv_image_dsc_t){
            .header = {.magic = LV_IMAGE_HEADER_MAGIC, .cf = LV_COLOR_FORMAT_RGB565, .w = size, .h = size,
                       .stride = size * 2},
            .data_size = size * size * 2,
            .data = s_px[i],
        };
    }
    s_wrap = lv_obj_create(parent);
    lv_obj_remove_style_all(s_wrap);
    lv_obj_set_size(s_wrap, size + 40, size + 40);
    lv_obj_remove_flag(s_wrap, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_wrap, LV_OBJ_FLAG_EVENT_BUBBLE);

    s_glow = lv_obj_create(s_wrap);   // faint disc behind the native orb
    lv_obj_remove_style_all(s_glow);
    lv_obj_set_size(s_glow, size, size);
    lv_obj_center(s_glow);
    lv_obj_set_style_radius(s_glow, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(s_glow, LV_OPA_20, 0);
    lv_obj_add_flag(s_glow, LV_OBJ_FLAG_EVENT_BUBBLE);

    s_core = lv_obj_create(s_wrap);
    lv_obj_remove_style_all(s_core);
    lv_obj_set_size(s_core, size / 2, size / 2);
    lv_obj_center(s_core);
    lv_obj_set_style_radius(s_core, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(s_core, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_grad_dir(s_core, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_shadow_spread(s_core, 2, 0);
    lv_obj_add_flag(s_core, LV_OBJ_FLAG_EVENT_BUBBLE);

    s_img = lv_image_create(s_wrap);
    lv_obj_center(s_img);
    lv_obj_add_flag(s_img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_img, LV_OBJ_FLAG_EVENT_BUBBLE);

    s_ring = lv_obj_create(s_wrap);
    lv_obj_remove_style_all(s_ring);
    lv_obj_set_size(s_ring, size + 14, size + 14);
    lv_obj_center(s_ring);
    lv_obj_set_style_radius(s_ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_shadow_width(s_ring, 24, 0);
    lv_obj_add_flag(s_ring, LV_OBJ_FLAG_EVENT_BUBBLE);

    s_arc = lv_arc_create(s_wrap);
    lv_obj_set_size(s_arc, size + 30, size + 30);
    lv_obj_center(s_arc);
    lv_arc_set_bg_angles(s_arc, 0, 360);
    lv_arc_set_angles(s_arc, 0, 70);
    lv_obj_remove_style(s_arc, NULL, LV_PART_KNOB);
    lv_obj_remove_flag(s_arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_arc_opa(s_arc, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_arc, 5, LV_PART_INDICATOR);
    lv_obj_add_flag(s_arc, LV_OBJ_FLAG_EVENT_BUBBLE);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, s_arc);
    lv_anim_set_exec_cb(&a, arc_spin);
    lv_anim_set_values(&a, 0, 360);
    lv_anim_set_duration(&a, 1100);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&a);

    s_native_timer = lv_timer_create(native_tick, 33, NULL);
    orb_apply_theme();
    xTaskCreatePinnedToCore(player_task, "orb", 4096, NULL, 2, &s_task, 0);  // internal: reads flash; core 0; prio 2 < LVGL draw thread (3, unpinned) so decoding never stalls drawing
}

lv_obj_t *orb_obj(void) { return s_wrap; }

void orb_set_state(pal_state_t st)
{
    s_state = st;
    if (s_ring) apply_state_style();
    if (s_task) xTaskNotifyGive(s_task);
}

void orb_set_active(bool on)
{
    if (on == s_active) return;
    s_active = on;
    if (s_native_timer) {
        if (on) lv_timer_resume(s_native_timer);
        else lv_timer_pause(s_native_timer);
    }
    if (on && s_task) xTaskNotifyGive(s_task);
}

static void set_native_visible(bool native)
{
    if (!pal_lock()) return;
    if (native) {
        lv_obj_add_flag(s_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_core, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_glow, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_ring, LV_OBJ_FLAG_HIDDEN);
    } else {
        // video orb: nothing else in its area, so each frame is a plain image
        // copy (the ring's circular glow made every frame several times slower)
        lv_obj_add_flag(s_core, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_glow, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_ring, LV_OBJ_FLAG_HIDDEN);
    }
    pal_unlock();
}

void orb_native(void)
{
    xSemaphoreTake(s_mu, portMAX_DELAY);
    for (int i = 0; i < 4; i++) clip_close(&s_clips[i]);
    s_have_clips = false;
    s_reload = true;
    xSemaphoreGive(s_mu);
    set_native_visible(true);
}

static void prune(cJSON *states)
{
    DIR *d = opendir(ORB_DIR);
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d))) {
        bool keep = e->d_name[0] == '_';   // _current.json (the boot cache)
        cJSON *s;
        cJSON_ArrayForEach(s, states) {
            const char *k = cJSON_GetStringValue(cJSON_GetObjectItem(s, "key"));
            if (k && !strncmp(e->d_name, k, strlen(k))) keep = true;
        }
        if (!keep) {
            char p[300];
            snprintf(p, sizeof(p), ORB_DIR "/%s", e->d_name);
            remove(p);
        }
    }
    closedir(d);
}

// The orb in use is remembered in ORB_DIR/_current.json, so a boot shows it
// straight from the flash cache instead of waiting for PAL.
#define ORB_CURRENT ORB_DIR "/_current.json"

static void save_current(cJSON *m)
{
    char *txt = cJSON_PrintUnformatted(m);
    if (!txt) return;
    FILE *f = fopen(ORB_CURRENT, "w");
    if (f) {
        fputs(txt, f);
        fclose(f);
    }
    cJSON_free(txt);
}

static void load_manifest(cJSON *m, bool allow_download);

void orb_load_cached(void)
{
    FILE *f = fopen(ORB_CURRENT, "r");
    if (!f) return;
    char *buf = heap_caps_malloc(4096, MALLOC_CAP_SPIRAM);
    size_t n = buf ? fread(buf, 1, 4095, f) : 0;
    fclose(f);
    if (!buf) return;
    buf[n] = 0;
    cJSON *m = cJSON_Parse(buf);
    free(buf);
    if (m) {
        ESP_LOGI(TAG, "boot: cached orb");
        load_manifest(m, false);
        cJSON_Delete(m);
    }
}

void orb_load_manifest(cJSON *m) { load_manifest(m, true); }

static void load_manifest(cJSON *m, bool allow_download)
{
    cJSON *states = cJSON_GetObjectItem(m, "states");
    if (!cJSON_IsObject(states) || !cJSON_GetObjectItem(states, "idle")) {
        if (!allow_download) return;
        ESP_LOGI(TAG, "no orb media — native orb");
        remove(ORB_CURRENT);
        orb_native();
        cJSON *none = cJSON_CreateObject();
        prune(none);
        cJSON_Delete(none);
        return;
    }
    // same clips as now? nothing to do (set_orb is replayed on every reconnect)
    static const char *names[4] = {"idle", "listening", "processing", "speaking"};
    bool same = s_have_clips;
    for (int i = 0; i < 4 && same; i++) {
        const char *key = cJSON_GetStringValue(cJSON_GetObjectItem(cJSON_GetObjectItem(states, names[i]), "key"));
        same = key && !strcmp(key, s_clips[i].key);
    }
    if (same) return;
    // download what's missing
    bool busy_shown = false;
    cJSON *s;
    cJSON_ArrayForEach(s, states) {
        const char *key = cJSON_GetStringValue(cJSON_GetObjectItem(s, "key"));
        const char *url = cJSON_GetStringValue(cJSON_GetObjectItem(s, "url"));
        if (!key || !url) continue;
        char path[64];
        snprintf(path, sizeof(path), ORB_DIR "/%s.palm", key);
        struct stat st;
        if (stat(path, &st) == 0 && st.st_size > 16) continue;
        if (!allow_download) return;   // cache incomplete: wait for PAL instead
        if (!busy_shown && pal_lock()) {   // real work ahead: say so
            ui_orb_busy(true);
            pal_unlock();
            busy_shown = true;
        }
        ESP_LOGI(TAG, "downloading %s", url);
        if (pal_get_to_file(url, path, 60000) != ESP_OK) ESP_LOGW(TAG, "download failed: %s", url);
    }
    clip_t next[4];
    bool any = false;
    for (int i = 0; i < 4; i++) {
        const char *key = cJSON_GetStringValue(cJSON_GetObjectItem(cJSON_GetObjectItem(states, names[i]), "key"));
        if (!key || !clip_open(&next[i], key)) memset(&next[i], 0, sizeof(clip_t));
        if (next[i].f && (next[i].w != s_size || next[i].h != s_size)) {
            ESP_LOGW(TAG, "clip %s is %dx%d, expected %d", key, next[i].w, next[i].h, s_size);
            clip_close(&next[i]);
        }
        any |= next[i].f != NULL;
    }
    xSemaphoreTake(s_mu, portMAX_DELAY);
    for (int i = 0; i < 4; i++) clip_close(&s_clips[i]);
    memcpy(s_clips, next, sizeof(next));
    s_have_clips = any && s_clips[PAL_IDLE].f;
    s_reload = true;
    xSemaphoreGive(s_mu);
    set_native_visible(!s_have_clips);
    if (allow_download) {
        prune(states);
        if (s_have_clips) save_current(m);
    }
    if (s_task) xTaskNotifyGive(s_task);
    ESP_LOGI(TAG, "orb %s: %s", cJSON_GetStringValue(cJSON_GetObjectItem(m, "name")),
             s_have_clips ? "clips" : "native");
}
