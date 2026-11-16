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
#include "esp_pm.h"
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
        printf("OK wifi=%s — rebooting\n", g_settings.wifi_ssid);
        fflush(stdout);
        app_post_str(EV_ACTION, "save_reboot");   // NVS writes need an internal stack
    } else if (!strcmp(argv[0], "wifi2") && argc >= 2) {
        // backup network ("wifi2 -" removes it); used when the main one is unreachable
        bool clear = !strcmp(argv[1], "-");
        strlcpy(g_settings.wifi2_ssid, clear ? "" : argv[1], sizeof(g_settings.wifi2_ssid));
        strlcpy(g_settings.wifi2_pass, !clear && argc > 2 ? argv[2] : "", sizeof(g_settings.wifi2_pass));
        app_post(EV_SETTINGS_CHANGED);
        printf("OK backup wifi=%s\n", clear ? "(none)" : g_settings.wifi2_ssid);
    } else if (!strcmp(argv[0], "net") && argc == 2 && !strcmp(argv[1], "check")) {
        app_post_str(EV_ACTION, "netcheck");   // the 10-minute "is the main network back?" check, now
        printf("OK checking\n");
    } else if (!strcmp(argv[0], "net") && argc == 2) {
        net_force(!strcmp(argv[1], "backup") ? 1 : 0);   // test the failover by hand
        printf("OK switching to the %s network\n", argv[1]);
    } else if (!strcmp(argv[0], "server") && argc == 2) {
        size_t l = strlen(argv[1]);
        while (l && argv[1][l - 1] == '/') argv[1][--l] = 0;
        strlcpy(g_settings.server, argv[1], sizeof(g_settings.server));
        app_post(EV_SETTINGS_CHANGED);
        printf("OK server=%s\n", g_settings.server);
        app_post(EV_WIFI_UP);   // re-run the connect flow
    } else if (!strcmp(argv[0], "ble") && argc == 2) {
        // Bluetooth link (BLE_LINK.md): pair = open the 2-min window, on/off = force
        // relay mode / back to Wi-Fi (tests), forget = delete the phone's bond
        const char *a = argv[1];
        if (!strcmp(a, "on")) app_post_str(EV_LINK, "ble");
        else if (!strcmp(a, "off")) app_post_str(EV_ACTION, "bleoff");
        else if (!strcmp(a, "pair")) app_post_str(EV_ACTION, "blepair");
        else if (!strcmp(a, "forget")) app_post_str(EV_ACTION, "bleforget");
        printf("OK ble %s (state=%s bonded=%d relay=%d streams=%d)\n", a, link_state(), g_settings.ble_bond,
               link_active(), link_streams());
    } else if (!strcmp(argv[0], "status")) {
        printf("device=%s fw=%s\nwifi=%s up=%d rssi=%d (main=%s backup=%s)\nserver=%s gateway=%s active=%s\npaired=%s dashboard=%s orb=%s\n"
               "ws=%d battery=%d%% charging=%d screen=%d playing=%d recording=%d\nble=%s bonded=%d relay=%d streams=%d\n",
               pal_device_name(), pal_fw_version(), net_ssid(), net_is_up(), net_rssi(), g_settings.wifi_ssid,
               g_settings.wifi2_ssid[0] ? g_settings.wifi2_ssid : "-", g_settings.server,
               g_settings.gateway, pal_base(), g_settings.token[0] ? "yes" : "no", g_settings.dashboard,
               g_settings.orb, ws_connected(), pmu_battery_pct(), pmu_charging(), ui_screen_on(), audio_playing(),
               audio_recording(), link_state(), g_settings.ble_bond, link_active(), link_streams());
    } else if (!strcmp(argv[0], "press")) {
        app_post(EV_PTT_PRESS);            // = a BOOT press (tests: "press" twice = voice note)
    } else if (!strcmp(argv[0], "wake")) {
        app_post_str(EV_ACTION, "wake");   // screen on (tests)
    } else if (!strcmp(argv[0], "beep")) {
        audio_beep(0);               // speaker path test (codec opens, plays, closes after 1.5 s)
    } else if (!strcmp(argv[0], "rec")) {
        audio_record_start(true);    // mic path test: records until a pause (result in the log)
    } else if (!strcmp(argv[0], "top")) {
        static char buf[1536];
        vTaskGetRunTimeStats(buf);   // cumulative CPU time per task since boot
        printf("%s", buf);
    } else if (!strcmp(argv[0], "pm")) {
        esp_pm_dump_locks(stdout);   // who keeps the CPU fast / awake
    } else if (!strcmp(argv[0], "unpair")) {
        app_post_str(EV_ACTION, "repair");
        printf("OK\n");
    } else if (!strcmp(argv[0], "reset")) {
        app_post_str(EV_ACTION, "reset");
    } else if (!strcmp(argv[0], "reboot")) {
        esp_restart();
    } else {
        printf("commands: wifi <ssid> [pass] | wifi2 <ssid> [pass] (backup; '-' clears) | net main|backup|check | ble pair|on|off|forget | server <url> | status | pm | top | wake | press | beep | rec | unpair | reset | reboot\n");
    }
    fflush(stdout);
}

static void console_task(void *arg)
{
    char line[200];
    size_t n = 0;
    while (true) {
        uint8_t ch;
        // blocking read: no polling, so the console never keeps the chip awake
        if (usb_serial_jtag_read_bytes(&ch, 1, portMAX_DELAY) != 1) continue;
        int c = ch;
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
    xTaskCreateWithCaps(console_task, "console", 4096, NULL, 3, NULL, MALLOC_CAP_SPIRAM);
}
