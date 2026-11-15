// Persistent settings in NVS (namespace "pal").
#include <string.h>
#include "app.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "settings";
pal_settings_t g_settings;

static void get_str(nvs_handle_t h, const char *key, char *dst, size_t len, const char *def)
{
    size_t n = len;
    if (nvs_get_str(h, key, dst, &n) != ESP_OK) {
        strlcpy(dst, def, len);
    }
}

static void defaults(void)
{
    memset(&g_settings, 0, sizeof(g_settings));
    strcpy(g_settings.dashboard, "");
    strcpy(g_settings.orb, "follow");
    g_settings.volume = 70;
    g_settings.brightness = 80;
    g_settings.screen_timeout_s = 30;
}

void settings_load(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    defaults();
    nvs_handle_t h;
    if (nvs_open("pal", NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "no saved settings");
        return;
    }
    get_str(h, "ssid", g_settings.wifi_ssid, sizeof(g_settings.wifi_ssid), "");
    get_str(h, "pass", g_settings.wifi_pass, sizeof(g_settings.wifi_pass), "");
    get_str(h, "ssid2", g_settings.wifi2_ssid, sizeof(g_settings.wifi2_ssid), "");
    get_str(h, "pass2", g_settings.wifi2_pass, sizeof(g_settings.wifi2_pass), "");
    get_str(h, "server", g_settings.server, sizeof(g_settings.server), "");
    get_str(h, "gateway", g_settings.gateway, sizeof(g_settings.gateway), "");
    get_str(h, "token", g_settings.token, sizeof(g_settings.token), "");
    get_str(h, "dash", g_settings.dashboard, sizeof(g_settings.dashboard), "");
    get_str(h, "orb", g_settings.orb, sizeof(g_settings.orb), "follow");
    uint8_t u8;
    uint16_t u16;
    if (nvs_get_u8(h, "vol", &u8) == ESP_OK) g_settings.volume = u8 > 100 ? 100 : u8;
    if (nvs_get_u8(h, "bright", &u8) == ESP_OK) g_settings.brightness = u8 < 5 ? 5 : (u8 > 100 ? 100 : u8);
    if (nvs_get_u16(h, "tmo", &u16) == ESP_OK) g_settings.screen_timeout_s = u16;
    nvs_close(h);
    ESP_LOGI(TAG, "wifi=%s backup=%s server=%s paired=%s dash=%s", g_settings.wifi_ssid, g_settings.wifi2_ssid[0] ? g_settings.wifi2_ssid : "-", g_settings.server,
             g_settings.token[0] ? "yes" : "no", g_settings.dashboard);
}

void settings_save(void)
{
    nvs_handle_t h;
    if (nvs_open("pal", NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open failed");
        return;
    }
    nvs_set_str(h, "ssid", g_settings.wifi_ssid);
    nvs_set_str(h, "pass", g_settings.wifi_pass);
    nvs_set_str(h, "ssid2", g_settings.wifi2_ssid);
    nvs_set_str(h, "pass2", g_settings.wifi2_pass);
    nvs_set_str(h, "server", g_settings.server);
    nvs_set_str(h, "gateway", g_settings.gateway);
    nvs_set_str(h, "token", g_settings.token);
    nvs_set_str(h, "dash", g_settings.dashboard);
    nvs_set_str(h, "orb", g_settings.orb);
    nvs_set_u8(h, "vol", g_settings.volume);
    nvs_set_u8(h, "bright", g_settings.brightness);
    nvs_set_u16(h, "tmo", g_settings.screen_timeout_s);
    nvs_commit(h);
    nvs_close(h);
}

void settings_factory_reset(void)
{
    nvs_handle_t h;
    if (nvs_open("pal", NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_all(h);
        nvs_commit(h);
        nvs_close(h);
    }
    defaults();
}
