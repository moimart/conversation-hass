// The satellite socket: /ws/ui?token=… on the active base. Carries the
// device's own turn state/replies, tts_play, set_orb/set_theme, weather,
// timers and the HA entity state stream. Fragmented frames are reassembled in
// PSRAM (ha_states snapshots can be tens of KB).
#include <string.h>
#include "app.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_websocket_client.h"

static const char *TAG = "ws";
static esp_websocket_client_handle_t s_ws;
static volatile bool s_up;
static char *s_buf;
static size_t s_cap;
static char s_uri[300];

#define WS_MAX_MSG (512 * 1024)

static void on_ws(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_websocket_event_data_t *d = data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "connected");
        s_up = true;
        app_post(EV_WS_UP);
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        if (s_up) {
            ESP_LOGW(TAG, "disconnected");
            s_up = false;
            app_post(EV_WS_DOWN);
        }
        break;
    case WEBSOCKET_EVENT_DATA:
        if (d->op_code != 0x1 && d->op_code != 0x0) break;  // text (+continuation)
        if (d->payload_len > WS_MAX_MSG) break;
        if ((size_t)d->payload_len + 1 > s_cap) {
            char *nb = heap_caps_realloc(s_buf, d->payload_len + 1, MALLOC_CAP_SPIRAM);
            if (!nb) break;
            s_buf = nb;
            s_cap = d->payload_len + 1;
        }
        memcpy(s_buf + d->payload_offset, d->data_ptr, d->data_len);
        if (d->payload_offset + d->data_len >= d->payload_len) {
            s_buf[d->payload_len] = 0;
            cJSON *j = cJSON_ParseWithLength(s_buf, d->payload_len);
            if (j) app_post_json(j);
        }
        break;
    default:
        break;
    }
}

void ws_start(void)
{
    ws_stop();
    const char *base = pal_base();
    const char *rest = base;
    const char *scheme = "ws://";
    if (!strncmp(base, "https://", 8)) { scheme = "wss://"; rest = base + 8; }
    else if (!strncmp(base, "http://", 7)) { rest = base + 7; }
    snprintf(s_uri, sizeof(s_uri), "%s%s/ws/ui?token=%s", scheme, rest, g_settings.token);
    esp_websocket_client_config_t c = {
        .uri = s_uri,
        .buffer_size = 4096,
        .task_stack = 6144,
        .reconnect_timeout_ms = 5000,
        .network_timeout_ms = 10000,
        .ping_interval_sec = 20,
        .pingpong_timeout_sec = 30,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    s_ws = esp_websocket_client_init(&c);
    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, on_ws, NULL);
    esp_websocket_client_start(s_ws);
}

void ws_stop(void)
{
    if (!s_ws) return;
    esp_websocket_client_stop(s_ws);
    esp_websocket_client_destroy(s_ws);
    s_ws = NULL;
    s_up = false;
}

bool ws_connected(void) { return s_up && s_ws && esp_websocket_client_is_connected(s_ws); }

void ws_send_text(const char *text)
{
    if (ws_connected()) esp_websocket_client_send_text(s_ws, text, strlen(text), pdMS_TO_TICKS(2000));
}

void ws_send_retain(const char *const *ids, int n)
{
    cJSON *m = cJSON_CreateObject();
    cJSON_AddStringToObject(m, "type", "ha_retain");
    cJSON *a = cJSON_AddArrayToObject(m, "ids");
    for (int i = 0; i < n; i++) cJSON_AddItemToArray(a, cJSON_CreateString(ids[i]));
    char *s = cJSON_PrintUnformatted(m);
    cJSON_Delete(m);
    if (s) {
        ws_send_text(s);
        free(s);
    }
}
