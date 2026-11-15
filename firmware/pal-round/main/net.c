// Wi-Fi station with auto-reconnect, SNTP time, and mDNS discovery of PAL
// (`_pal._tcp`, advertised by the hal-mdns container on the AI server).
#include <string.h>
#include <time.h>
#include "app.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"
#include "mdns.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "net";
static volatile bool s_up;
static bool s_sntp;
static esp_netif_t *s_sta;

// Main + backup network. After MAX_FAILS failed attempts on one, the station
// switches to the other. While on the backup, net_check_main() (every 10 min
// from the app task) scans for the main network and moves back when it's in range.
#define MAX_FAILS 4
static int s_net;            // 0 = main, 1 = backup
static int s_fails;

static bool has_backup(void) { return g_settings.wifi2_ssid[0] != 0; }

static void apply_net(int idx)
{
    const char *ssid = idx ? g_settings.wifi2_ssid : g_settings.wifi_ssid;
    const char *pass = idx ? g_settings.wifi2_pass : g_settings.wifi_pass;
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, pass, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable = true;
    wc.sta.listen_interval = 3;   // used only in MAX_MODEM (screen off): wake every 3rd beacon
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    s_net = idx;
    s_fails = 0;
    ESP_LOGI(TAG, "using %s network \"%s\"", idx ? "backup" : "main", ssid);
}

const char *net_ssid(void) { return s_net ? g_settings.wifi2_ssid : g_settings.wifi_ssid; }
bool net_on_backup(void) { return s_net == 1; }

void net_force(int idx)   // console test: jump to the main (0) / backup (1) network now
{
    if (idx == 1 && !has_backup()) return;
    apply_net(idx);
    esp_wifi_disconnect();
}

void net_check_main(void)
{
    if (s_net != 1 || !g_settings.wifi_ssid[0] || portal_active()) return;
    wifi_scan_config_t sc = {.ssid = (uint8_t *)g_settings.wifi_ssid, .show_hidden = true};
    if (esp_wifi_scan_start(&sc, true) != ESP_OK) return;
    uint16_t n = 1;
    wifi_ap_record_t ap;
    bool found = esp_wifi_scan_get_ap_records(&n, &ap) == ESP_OK && n > 0 && ap.rssi > -82;
    esp_wifi_clear_ap_list();
    if (!found) return;
    ESP_LOGI(TAG, "main network back in range (%d dBm): switching", ap.rssi);
    apply_net(0);
    esp_wifi_disconnect();   // the DISCONNECTED handler reconnects with the main config
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (g_settings.wifi_ssid[0]) esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_up) app_post(EV_WIFI_DOWN);
        s_up = false;
        if (g_settings.wifi_ssid[0] && !portal_active()) {
            wifi_event_sta_disconnected_t *d = data;
            if (++s_fails >= MAX_FAILS && has_backup()) {
                // reason 201 = not found (out of range / 5 GHz only), 15/204 = wrong password
                ESP_LOGW(TAG, "\"%s\" unreachable after %d tries (reason %d)", net_ssid(), MAX_FAILS,
                         d ? d->reason : -1);
                apply_net(s_net ^ 1);
            }
            vTaskDelay(pdMS_TO_TICKS(1500));
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
        s_up = true;
        s_fails = 0;
        if (!s_sntp) {
            esp_sntp_config_t c = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
            esp_netif_sntp_init(&c);
            s_sntp = true;
        }
        app_post(EV_WIFI_UP);
    }
}

void net_start(void)
{
    esp_netif_init();
    esp_event_loop_create_default();
    s_sta = esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL);
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    char host[32];
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);
    snprintf(host, sizeof(host), "pal-round-%02x%02x", mac[4], mac[5]);
    esp_netif_set_hostname(s_sta, host);
    mdns_init();
    mdns_hostname_set(host);
    if (!g_settings.wifi_ssid[0]) {
        ESP_LOGW(TAG, "no Wi-Fi configured");
        return;
    }
    esp_wifi_set_mode(WIFI_MODE_STA);
    apply_net(0);
    esp_wifi_start();
    // modem sleep between DTIM beacons: keeps the socket up at a fraction of the power
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
}

bool net_is_up(void) { return s_up; }

void net_power_save(bool deep)
{
    // screen on: wake for every DTIM beacon (snappy); screen off: every 3rd
    // beacon (~300 ms). The socket stays up either way.
    esp_wifi_set_ps(deep ? WIFI_PS_MAX_MODEM : WIFI_PS_MIN_MODEM);
}

int net_rssi(void)
{
    wifi_ap_record_t ap;
    return (s_up && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : -127;
}

bool net_discover_pal(char *base, size_t len)
{
    mdns_result_t *res = NULL;
    if (mdns_query_ptr("_pal", "_tcp", 3000, 4, &res) != ESP_OK || !res) return false;
    bool ok = false;
    for (mdns_result_t *r = res; r && !ok; r = r->next) {
        const char *scheme = "http";
        for (size_t i = 0; i < r->txt_count; i++) {
            if (!strcmp(r->txt[i].key, "scheme") && r->txt[i].value) scheme = r->txt[i].value;
        }
        for (mdns_ip_addr_t *a = r->addr; a; a = a->next) {
            if (a->addr.type == ESP_IPADDR_TYPE_V4) {
                snprintf(base, len, "%s://" IPSTR ":%u", scheme, IP2STR(&a->addr.u_addr.ip4), r->port);
                ok = true;
                break;
            }
        }
    }
    mdns_query_results_free(res);
    if (ok) ESP_LOGI(TAG, "discovered PAL at %s", base);
    return ok;
}

void net_set_tz(const char *posix_tz)
{
    if (!posix_tz || !posix_tz[0]) return;
    setenv("TZ", posix_tz, 1);
    tzset();
}

bool net_time_valid(void)
{
    time_t now = time(NULL);
    return now > 1700000000;
}
