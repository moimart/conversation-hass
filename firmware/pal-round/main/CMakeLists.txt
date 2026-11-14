idf_component_register(
    SRCS "main.c" "lv_mem_psram.c" "settings.c" "fonts.c" "pmu.c" "audio.c" "net.c" "portal.c" "pal_client.c"
         "pal_ws.c" "orb.c" "ha.c" "ui.c" "console.c"
    INCLUDE_DIRS "."
    WHOLE_ARCHIVE
    EMBED_FILES "assets/inter.ttf" "assets/mdi.ttf"
    REQUIRES json nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_http_server app_update
             esp_app_format esp_timer fatfs wear_levelling driver esp_driver_gpio esp_driver_usb_serial_jtag
             esp_driver_i2c lwip mbedtls esp_psram esp32_s3_touch_amoled_1_75c
)
