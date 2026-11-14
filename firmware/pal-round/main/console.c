// USB serial provisioning (the same USB-C port used for flashing). One
// command per line:
//   wifi <ssid> [password]     join a network (reboots)
//   server <url>               PAL's LAN base, e.g. http://10.20.30.185:8765
//   status                     print the configuration (token redacted)
//   unpair | reset | reboot
// SSIDs with spaces: wrap in double quotes.
#include <stdio.h>
#include <string.h>
#include "app.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "console";

// split "a b" / "\"a b\" c" into up to 3 args (in place)
static int split(char *s, char *argv[3])
{
    int n = 0;
    while (*s && n < 3) {
        while (*s == ' ') s++;
        if (!*s) break;
        if (*s == '"') {
            argv[n++] = ++s;
            while (*s && *s != '"') s++;
        } else {
            argv[n++] = s;
            while (*s && *s != ' ') s++;
        }
        if (*s) *s++ = 0;
    }
    return n;
}

static void handle(char *line)
{
    char *argv[3] = {0};
    int argc = split(line, argv);
    if (!argc) return;
    if (!strcmp(argv[0], "wifi") && argc >= 2) {
        strlcpy(g_settings.wifi_ssid, argv[1], sizeof(g_settings.wifi_ssid));
        strlcpy(g_settings.wifi_pass, argc > 2 ? argv[2] : "", sizeof(g_settings.wifi_pass));
        settings_save();
        printf("OK wifi=%s — rebooting\n", g_settings.wifi_ssid);
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
    } else if (!strcmp(argv[0], "server") && argc == 2) {
        size_t l = strlen(argv[1]);
        while (l && argv[1][l - 1] == '/') argv[1][--l] = 0;
        strlcpy(g_settings.server, argv[1], sizeof(g_settings.server));
        settings_save();
        printf("OK server=%s\n", g_settings.server);
        app_post(EV_WIFI_UP);   // re-run the connect flow
    } else if (!strcmp(argv[0], "status")) {
        printf("device=%s fw=%s\nwifi=%s up=%d rssi=%d\nserver=%s gateway=%s active=%s\npaired=%s dashboard=%s orb=%s\n"
               "ws=%d battery=%d%% charging=%d\n",
               pal_device_name(), pal_fw_version(), g_settings.wifi_ssid, net_is_up(), net_rssi(), g_settings.server,
               g_settings.gateway, pal_base(), g_settings.token[0] ? "yes" : "no", g_settings.dashboard,
               g_settings.orb, ws_connected(), pmu_battery_pct(), pmu_charging());
    } else if (!strcmp(argv[0], "unpair")) {
        app_post_str(EV_ACTION, "repair");
        printf("OK\n");
    } else if (!strcmp(argv[0], "reset")) {
        app_post_str(EV_ACTION, "reset");
    } else if (!strcmp(argv[0], "reboot")) {
        esp_restart();
    } else {
        printf("commands: wifi <ssid> [pass] | server <url> | status | unpair | reset | reboot\n");
    }
    fflush(stdout);
}

static void console_task(void *arg)
{
    char line[200];
    size_t n = 0;
    while (true) {
        int c = fgetc(stdin);
        if (c == EOF) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        if (c == '\r') continue;
        if (c == '\n') {
            line[n] = 0;
            handle(line);
            n = 0;
        } else if (n < sizeof(line) - 1) {
            line[n++] = (char)c;
        }
    }
}

void console_start(void)
{
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    if (usb_serial_jtag_driver_install(&cfg) != ESP_OK) {
        ESP_LOGW(TAG, "usb serial driver failed");
        return;
    }
    usb_serial_jtag_vfs_use_driver();
    setvbuf(stdin, NULL, _IONBF, 0);
    xTaskCreateWithCaps(console_task, "console", 4096, NULL, 3, NULL, MALLOC_CAP_SPIRAM);
}
