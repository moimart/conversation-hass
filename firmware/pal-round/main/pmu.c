// AXP2101 power management: battery level, charging, VBUS and the PWR key.
// The PMU's IRQ line isn't wired to the ESP32, so a small task polls the
// latched IRQ status for short/long key presses.
#include "app.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "bsp/esp-bsp.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "pmu";
static i2c_master_dev_handle_t s_dev;
static bool s_ok;

#define REG_STATUS1 0x00
#define REG_STATUS2 0x01
#define REG_PWROFF 0x10
#define REG_ADC_CTRL 0x30
#define REG_INTEN1 0x40
#define REG_INTSTS1 0x48
#define REG_BAT_PCT 0xA4
#define IRQ2_PKEY_LONG (1 << 2)   // bit 10
#define IRQ2_PKEY_SHORT (1 << 3)  // bit 11

static esp_err_t rd(uint8_t reg, uint8_t *v)
{
    return i2c_master_transmit_receive(s_dev, &reg, 1, v, 1, 100);
}

static esp_err_t wr(uint8_t reg, uint8_t v)
{
    uint8_t b[2] = {reg, v};
    return i2c_master_transmit(s_dev, b, 2, 100);
}

static void pmu_task(void *arg)
{
    while (true) {
        uint8_t st = 0;
        if (rd(REG_INTSTS1 + 1, &st) == ESP_OK && st) {
            wr(REG_INTSTS1 + 1, st);  // write-1-to-clear
            if (st & IRQ2_PKEY_SHORT) app_post(EV_PWR_SHORT);
            if (st & IRQ2_PKEY_LONG) app_post(EV_PWR_LONG);
        }
        vTaskDelay(pdMS_TO_TICKS(120));
    }
}

void pmu_init(void)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x34, .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &cfg, &s_dev) != ESP_OK) {
        ESP_LOGE(TAG, "no AXP2101");
        return;
    }
    uint8_t v;
    if (rd(REG_STATUS1, &v) != ESP_OK) {
        ESP_LOGE(TAG, "AXP2101 not responding");
        return;
    }
    s_ok = true;
    // ADCs: battery voltage, VBUS, system voltage, temperature (fuel gauge input)
    if (rd(REG_ADC_CTRL, &v) == ESP_OK) wr(REG_ADC_CTRL, v | 0x1D);
    // only the key IRQs; clear anything latched
    wr(REG_INTEN1, 0x00);
    wr(REG_INTEN1 + 1, IRQ2_PKEY_LONG | IRQ2_PKEY_SHORT);
    wr(REG_INTEN1 + 2, 0x00);
    for (int i = 0; i < 3; i++) wr(REG_INTSTS1 + i, 0xFF);
    ESP_LOGI(TAG, "battery %d%% charging=%d vbus=%d", pmu_battery_pct(), pmu_charging(), pmu_vbus());
    xTaskCreateWithCaps(pmu_task, "pmu", 3072, NULL, 3, NULL, MALLOC_CAP_SPIRAM);
}

int pmu_battery_pct(void)
{
    uint8_t s1, pct;
    if (!s_ok || rd(REG_STATUS1, &s1) != ESP_OK || !(s1 & (1 << 3))) return -1;
    return rd(REG_BAT_PCT, &pct) == ESP_OK ? (pct > 100 ? 100 : pct) : -1;
}

bool pmu_charging(void)
{
    uint8_t s2;
    return s_ok && rd(REG_STATUS2, &s2) == ESP_OK && ((s2 >> 5) & 0x3) == 0x1;
}

bool pmu_vbus(void)
{
    uint8_t s1, s2;
    return s_ok && rd(REG_STATUS1, &s1) == ESP_OK && (s1 & (1 << 5)) && rd(REG_STATUS2, &s2) == ESP_OK &&
           !(s2 & (1 << 3));
}

void pmu_power_off(void)
{
    uint8_t v;
    if (s_ok && rd(REG_PWROFF, &v) == ESP_OK) {
        ESP_LOGW(TAG, "power off");
        wr(REG_PWROFF, v | 0x01);
    }
}
