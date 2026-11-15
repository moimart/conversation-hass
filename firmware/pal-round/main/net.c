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

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (g_settings.wifi_ssid[0]) esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_up) app_post(EV_WIFI_DOWN);
        s_up = false;
        if (g_settings.wifi_ssid[0] && !portal_active()) {
            vTaskDelay(pdMS_TO_TICKS(1500));
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
        s_up = true;
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
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, g_settings.wifi_ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, g_settings.wifi_pass, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = g_settings.wifi_pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable = true;
    wc.sta.listen_interval = 3;   // used only in MAX_MODEM (screen off): wake every 3rd beacon
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wc);
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
