// First-run setup without a computer: the device opens an open Wi-Fi network
// "PAL-Round-XXXX", answers every DNS query with itself (so phones pop the
// captive-portal sheet) and serves one form: Wi-Fi network + password +
// (optional) PAL server URL. Saving reboots into normal mode.
#include <ctype.h>
#include <string.h>
#include "app.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

static const char *TAG = "portal";
static bool s_active;
static char s_ssid[32];
static char *s_options;  // <option> list of scanned networks

const char *portal_ssid(void) { return s_ssid; }
bool portal_active(void) { return s_active; }

// --- captive DNS: every A query → 192.168.4.1 ----------------------------------

static void dns_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(53), .sin_addr.s_addr = htonl(INADDR_ANY)};
    bind(sock, (struct sockaddr *)&addr, sizeof(addr));
    uint8_t buf[512];
    while (true) {
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        int n = recvfrom(sock, buf, sizeof(buf) - 16, 0, (struct sockaddr *)&from, &fl);
        if (n < 12) continue;
        buf[2] |= 0x80;            // response
        buf[3] = 0x80;             // recursion available, no error
        buf[6] = 0; buf[7] = 1;    // one answer
        buf[8] = buf[9] = buf[10] = buf[11] = 0;
        uint8_t ans[] = {0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4, 192, 168, 4, 1};
        memcpy(buf + n, ans, sizeof(ans));
        sendto(sock, buf, n + sizeof(ans), 0, (struct sockaddr *)&from, fl);
    }
}

// --- HTTP ----------------------------------------------------------------------

static const char *PAGE_HEAD =
    "<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>PAL Round setup</title><style>body{font-family:system-ui,sans-serif;background:#0a0a0c;color:#eee;"
    "max-width:420px;margin:24px auto;padding:0 16px}input,select,button{width:100%;box-sizing:border-box;"
    "font-size:17px;padding:12px;margin:6px 0 14px;border-radius:10px;border:1px solid #333;background:#16161a;color:#eee}"
    "button{background:#ff8050;color:#000;font-weight:600;border:0}small{color:#888}</style></head><body>"
    "<h2>PAL Round</h2>";

static esp_err_t h_root(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    httpd_resp_sendstr_chunk(req,
        "<form method=post action=/save><label>Wi-Fi network</label><select name=ssid>");
    httpd_resp_sendstr_chunk(req, s_options ? s_options : "");
    httpd_resp_sendstr_chunk(req,
        "</select><small>Not listed? Type it:</small><input name=ssid2 placeholder='Network name'>"
        "<label>Password</label><input name=pass type=password>"
        "<label>Backup network <small>(optional — used if the first is unreachable)</small></label>"
        "<input name=bssid placeholder='Network name'><input name=bpass type=password placeholder='Password'>"
        "<label>PAL server <small>(optional — found automatically)</small></label>"
        "<input name=server placeholder='http://10.20.30.185:8765'>"
        "<button>Save &amp; restart</button></form></body></html>");
    httpd_resp_sendstr_chunk(req, NULL);
    return ESP_OK;
}

static void url_decode(char *s)
{
    char *o = s;
    for (; *s; s++, o++) {
        if (*s == '+') *o = ' ';
        else if (*s == '%' && isxdigit((int)s[1]) && isxdigit((int)s[2])) {
            char h[3] = {s[1], s[2], 0};
            *o = (char)strtol(h, NULL, 16);
            s += 2;
        } else *o = *s;
    }
    *o = 0;
}

static void form_get(const char *body, const char *key, char *out, size_t len)
{
    out[0] = 0;
    if (httpd_query_key_value(body, key, out, len) == ESP_OK) url_decode(out);
}

static void reboot_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

static esp_err_t h_save(httpd_req_t *req)
{
    char body[768] = {0};
    int n = httpd_req_recv(req, body, sizeof(body) - 1);
    if (n <= 0) return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "empty");
    body[n] = 0;
    char ssid[33], ssid2[33], pass[65], server[128], bssid[33], bpass[65];
    form_get(body, "ssid", ssid, sizeof(ssid));
    form_get(body, "ssid2", ssid2, sizeof(ssid2));
    form_get(body, "pass", pass, sizeof(pass));
    form_get(body, "server", server, sizeof(server));
    form_get(body, "bssid", bssid, sizeof(bssid));
    form_get(body, "bpass", bpass, sizeof(bpass));
    if (bssid[0]) {
        strlcpy(g_settings.wifi2_ssid, bssid, sizeof(g_settings.wifi2_ssid));
        strlcpy(g_settings.wifi2_pass, bpass, sizeof(g_settings.wifi2_pass));
    }
    const char *use = ssid2[0] ? ssid2 : ssid;
    if (!use[0]) return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "pick a network");
    strlcpy(g_settings.wifi_ssid, use, sizeof(g_settings.wifi_ssid));
    strlcpy(g_settings.wifi_pass, pass, sizeof(g_settings.wifi_pass));
    if (server[0]) {
        size_t l = strlen(server);
        while (l && server[l - 1] == '/') server[--l] = 0;
        strlcpy(g_settings.server, server, sizeof(g_settings.server));
    }
    settings_save();
    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    httpd_resp_sendstr_chunk(req, "<p>Saved. PAL Round is restarting and joining your network — "
                                  "you can close this page.</p></body></html>");
    httpd_resp_sendstr_chunk(req, NULL);
    xTaskCreate(reboot_task, "reboot", 2048, NULL, 5, NULL);
    return ESP_OK;
}

static esp_err_t h_redirect(httpd_req_t *req, httpd_err_code_t err)
{
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

static void scan_networks(void)
{
    wifi_scan_config_t sc = {0};
    if (esp_wifi_scan_start(&sc, true) != ESP_OK) return;
    uint16_t n = 20;
    wifi_ap_record_t *aps = calloc(n, sizeof(*aps));
    if (!aps) return;
    esp_wifi_scan_get_ap_records(&n, aps);
    size_t cap = 64 + n * 96;
    s_options = calloc(1, cap);
    size_t pos = 0;
    for (int i = 0; i < n && s_options; i++) {
        const char *name = (const char *)aps[i].ssid;
        if (!name[0]) continue;
        bool dup = false;
        for (int j = 0; j < i; j++) dup |= !strcmp(name, (const char *)aps[j].ssid);
        if (dup) continue;
        char esc[70] = {0};
        for (size_t k = 0, e = 0; name[k] && e < sizeof(esc) - 6; k++) {
            if (name[k] == '\'' || name[k] == '<' || name[k] == '&') { e += snprintf(esc + e, 6, "&#%d;", name[k]); }
            else esc[e++] = name[k];
        }
        pos += snprintf(s_options + pos, cap - pos, "<option value='%s'>%s (%d dBm)</option>", esc, esc, aps[i].rssi);
    }
    free(aps);
}

void portal_start(void)
{
    if (s_active) return;
    s_active = true;
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_AP, mac);
    snprintf(s_ssid, sizeof(s_ssid), "PAL-Round-%02X%02X", mac[4], mac[5]);
    wifi_config_t ap = {0};
    strlcpy((char *)ap.ap.ssid, s_ssid, sizeof(ap.ap.ssid));
    ap.ap.ssid_len = strlen(s_ssid);
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    if (net_ble_mode()) net_leave_ble(0);
    net_wifi_ensure();
    esp_wifi_stop();
    esp_wifi_set_mode(WIFI_MODE_APSTA);
    esp_wifi_set_config(WIFI_IF_AP, &ap);
    esp_wifi_start();
    scan_networks();
    xTaskCreate(dns_task, "dns", 3072, NULL, 4, NULL);
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.uri_match_fn = httpd_uri_match_wildcard;
    httpd_handle_t srv;
    if (httpd_start(&srv, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "httpd failed");
        return;
    }
    httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = h_root};
    httpd_uri_t save = {.uri = "/save", .method = HTTP_POST, .handler = h_save};
    httpd_register_uri_handler(srv, &root);
    httpd_register_uri_handler(srv, &save);
    httpd_register_err_handler(srv, HTTPD_404_NOT_FOUND, h_redirect);
    ESP_LOGI(TAG, "setup portal on %s", s_ssid);
}
