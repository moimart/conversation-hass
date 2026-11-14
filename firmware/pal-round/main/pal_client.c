// HTTP to PAL: Bearer-token requests against the active base. At home that's
// the LAN server; when it isn't reachable (the device left the house) and a
// gateway URL was handed out at pairing, the gateway (HTTPS, via Cloudflare).
#include <stdio.h>
#include <string.h>
#include "app.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "pal";
static char s_base[128];

const char *pal_base(void) { return s_base[0] ? s_base : g_settings.server; }

static esp_http_client_handle_t open_client(const char *base, const char *path, esp_http_client_method_t m,
                                            int timeout_ms)
{
    char url[320];
    snprintf(url, sizeof(url), "%s%s", base, path);
    esp_http_client_config_t c = {
        .url = url, .method = m, .timeout_ms = timeout_ms, .buffer_size = 2048, .buffer_size_tx = 1536,
        .crt_bundle_attach = esp_crt_bundle_attach, .keep_alive_enable = true,
    };
    esp_http_client_handle_t h = esp_http_client_init(&c);
    if (h && g_settings.token[0]) {
        char auth[120];
        snprintf(auth, sizeof(auth), "Bearer %s", g_settings.token);
        esp_http_client_set_header(h, "Authorization", auth);
    }
    return h;
}

static bool probe(const char *base)
{
    if (!base || !base[0]) return false;
    esp_http_client_handle_t h = open_client(base, "/health", HTTP_METHOD_GET, 2500);
    if (!h) return false;
    bool ok = esp_http_client_perform(h) == ESP_OK && esp_http_client_get_status_code(h) == 200;
    esp_http_client_cleanup(h);
    return ok;
}

void pal_choose_base(void)
{
    if (probe(g_settings.server)) {
        strlcpy(s_base, g_settings.server, sizeof(s_base));
    } else if (g_settings.gateway[0] && probe(g_settings.gateway)) {
        strlcpy(s_base, g_settings.gateway, sizeof(s_base));
    } else {
        strlcpy(s_base, g_settings.server, sizeof(s_base));
    }
    ESP_LOGI(TAG, "using %s", s_base);
}

// Read the whole response into a NUL-terminated PSRAM buffer.
static esp_err_t read_all(esp_http_client_handle_t h, pal_resp_t *out)
{
    int64_t cl = esp_http_client_fetch_headers(h);
    out->status = esp_http_client_get_status_code(h);
    size_t cap = cl > 0 ? (size_t)cl + 1 : 4096;
    char *buf = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
    if (!buf) return ESP_ERR_NO_MEM;
    size_t n = 0;
    while (true) {
        if (n + 1024 + 1 > cap) {
            size_t ncap = cap * 2;
            char *nb = heap_caps_realloc(buf, ncap, MALLOC_CAP_SPIRAM);
            if (!nb) { free(buf); return ESP_ERR_NO_MEM; }
            buf = nb;
            cap = ncap;
        }
        int r = esp_http_client_read(h, buf + n, cap - n - 1);
        if (r < 0) { free(buf); return ESP_FAIL; }
        if (r == 0) break;
        n += r;
    }
    buf[n] = 0;
    out->body = buf;
    out->len = n;
    return ESP_OK;
}

static esp_err_t request(const char *path, esp_http_client_method_t m, const void *body, size_t blen,
                         const char *ctype, const char *xh, const char *xv, pal_resp_t *out, int timeout_ms)
{
    memset(out, 0, sizeof(*out));
    esp_http_client_handle_t h = open_client(pal_base(), path, m, timeout_ms);
    if (!h) return ESP_FAIL;
    if (ctype) esp_http_client_set_header(h, "Content-Type", ctype);
    if (xh) esp_http_client_set_header(h, xh, xv);
    esp_err_t err = esp_http_client_open(h, body ? blen : 0);
    if (err == ESP_OK && body && blen) {
        size_t off = 0;
        while (off < blen) {
            int w = esp_http_client_write(h, (const char *)body + off, blen - off);
            if (w <= 0) { err = ESP_FAIL; break; }
            off += w;
        }
    }
    if (err == ESP_OK) err = read_all(h, out);
    esp_http_client_close(h);
    esp_http_client_cleanup(h);
    if (err != ESP_OK) ESP_LOGW(TAG, "%s failed: %s", path, esp_err_to_name(err));
    return err;
}

esp_err_t pal_get(const char *path, pal_resp_t *out, int timeout_ms)
{
    return request(path, HTTP_METHOD_GET, NULL, 0, NULL, NULL, NULL, out, timeout_ms);
}

esp_err_t pal_post_json(const char *path, const char *json, pal_resp_t *out, int timeout_ms)
{
    return request(path, HTTP_METHOD_POST, json, strlen(json), "application/json", NULL, NULL, out, timeout_ms);
}

esp_err_t pal_post_bin(const char *path, const void *data, size_t len, const char *ctype, const char *xh,
                       const char *xv, pal_resp_t *out, int timeout_ms)
{
    return request(path, HTTP_METHOD_POST, data, len, ctype, xh, xv, out, timeout_ms);
}

void pal_resp_free(pal_resp_t *r)
{
    free(r->body);
    r->body = NULL;
}

cJSON *pal_get_json(const char *path, int timeout_ms)
{
    pal_resp_t r;
    if (pal_get(path, &r, timeout_ms) != ESP_OK) return NULL;
    cJSON *j = (r.status == 200) ? cJSON_Parse(r.body) : NULL;
    if (r.status != 200) ESP_LOGW(TAG, "GET %s -> %d", path, r.status);
    pal_resp_free(&r);
    return j;
}

esp_err_t pal_get_to_file(const char *path, const char *file, int timeout_ms)
{
    esp_http_client_handle_t h = open_client(pal_base(), path, HTTP_METHOD_GET, timeout_ms);
    if (!h) return ESP_FAIL;
    esp_err_t err = esp_http_client_open(h, 0);
    if (err == ESP_OK) {
        esp_http_client_fetch_headers(h);
        if (esp_http_client_get_status_code(h) != 200) err = ESP_ERR_NOT_FOUND;
    }
    char tmp[96];
    snprintf(tmp, sizeof(tmp), "%s.part", file);
    FILE *f = err == ESP_OK ? fopen(tmp, "wb") : NULL;
    if (err == ESP_OK && !f) err = ESP_FAIL;
    if (f) {
        char *buf = malloc(4096);
        int r;
        while (buf && (r = esp_http_client_read(h, buf, 4096)) > 0) {
            if (fwrite(buf, 1, r, f) != (size_t)r) { err = ESP_FAIL; break; }
        }
        if (!buf || r < 0) err = ESP_FAIL;
        free(buf);
        fclose(f);
        if (err == ESP_OK) {
            remove(file);
            if (rename(tmp, file) != 0) err = ESP_FAIL;
        } else {
            remove(tmp);
        }
    }
    esp_http_client_close(h);
    esp_http_client_cleanup(h);
    return err;
}

// HA service calls run on their own task: they're triggered from LVGL event
// callbacks (which hold the display lock) and must never block the UI.
static QueueHandle_t s_svc_q;

static void svc_task(void *arg)
{
    char *body;
    while (true) {
        if (xQueueReceive(s_svc_q, &body, portMAX_DELAY) != pdTRUE) continue;
        pal_resp_t r;
        if (pal_post_json("/api/ha/service", body, &r, 10000) == ESP_OK) {
            if (r.status != 200) ESP_LOGW(TAG, "service %s -> %d %s", body, r.status, r.body ? r.body : "");
            pal_resp_free(&r);
        }
        cJSON_free(body);
    }
}

esp_err_t pal_call_service(const char *domain, const char *service, const char *entity, cJSON *data)
{
    if (!s_svc_q) {
        s_svc_q = xQueueCreate(8, sizeof(char *));
        xTaskCreate(svc_task, "svc", 6144, NULL, 4, NULL);
    }
    cJSON *b = cJSON_CreateObject();
    cJSON_AddStringToObject(b, "domain", domain);
    cJSON_AddStringToObject(b, "service", service);
    cJSON_AddItemToObject(b, "data", data ? data : cJSON_CreateObject());
    cJSON *t = cJSON_AddObjectToObject(b, "target");
    cJSON_AddStringToObject(t, "entity_id", entity);
    char *s = cJSON_PrintUnformatted(b);
    cJSON_Delete(b);
    if (!s) return ESP_ERR_NO_MEM;
    if (xQueueSend(s_svc_q, &s, 0) != pdTRUE) {
        cJSON_free(s);
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t pal_ota_update(const char *path, void (*progress)(int pct))
{
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) return ESP_FAIL;
    esp_http_client_handle_t h = open_client(pal_base(), path, HTTP_METHOD_GET, 30000);
    if (!h) return ESP_FAIL;
    esp_err_t err = esp_http_client_open(h, 0);
    int64_t total = err == ESP_OK ? esp_http_client_fetch_headers(h) : 0;
    if (err == ESP_OK && esp_http_client_get_status_code(h) != 200) err = ESP_ERR_NOT_FOUND;
    esp_ota_handle_t ota = 0;
    if (err == ESP_OK) err = esp_ota_begin(part, OTA_SIZE_UNKNOWN, &ota);
    char *buf = malloc(4096);
    int64_t done = 0;
    int last = -1;
    while (err == ESP_OK && buf) {
        int r = esp_http_client_read(h, buf, 4096);
        if (r < 0) { err = ESP_FAIL; break; }
        if (r == 0) break;
        err = esp_ota_write(ota, buf, r);
        done += r;
        int pct = total > 0 ? (int)(done * 100 / total) : 0;
        if (progress && pct != last) { progress(pct); last = pct; }
    }
    free(buf);
    if (ota) {
        if (err == ESP_OK) err = esp_ota_end(ota);
        else esp_ota_abort(ota);
    }
    if (err == ESP_OK) err = esp_ota_set_boot_partition(part);
    esp_http_client_close(h);
    esp_http_client_cleanup(h);
    ESP_LOGI(TAG, "OTA %s (%lld bytes)", esp_err_to_name(err), (long long)done);
    return err;
}
