// Fonts: rendered at runtime by FreeType from the TTFs embedded in the image
// (Inter for text, Material Design Icons for icons — the same icon set Home
// Assistant uses, so entity icons map 1:1).
#include "app.h"
#include "esp_log.h"
#include "esp_lv_adapter.h"

static const char *TAG = "fonts";
pal_fonts_t g_fonts;

extern const uint8_t inter_ttf_start[] asm("_binary_inter_ttf_start");
extern const uint8_t inter_ttf_end[] asm("_binary_inter_ttf_end");
extern const uint8_t mdi_ttf_start[] asm("_binary_mdi_ttf_start");
extern const uint8_t mdi_ttf_end[] asm("_binary_mdi_ttf_end");

static const lv_font_t *load(const char *name, const uint8_t *start, const uint8_t *end, int size,
                             esp_lv_adapter_ft_font_style_t style, const lv_font_t *fallback)
{
    esp_lv_adapter_ft_font_config_t cfg = {
        .name = name, .size = size, .style = style, .mem = start, .mem_size = (size_t)(end - start),
    };
    esp_lv_adapter_ft_font_handle_t h = NULL;
    if (esp_lv_adapter_ft_font_init(&cfg, &h) != ESP_OK || !h) {
        ESP_LOGE(TAG, "font %s/%d failed", name, size);
        return fallback;
    }
    return esp_lv_adapter_ft_font_get(h);
}

void fonts_init(void)
{
    const lv_font_t *fb = &lv_font_montserrat_14;
    g_fonts.text16 = load("inter", inter_ttf_start, inter_ttf_end, 17, ESP_LV_ADAPTER_FT_FONT_STYLE_NORMAL, fb);
    g_fonts.text20 = load("inter", inter_ttf_start, inter_ttf_end, 21, ESP_LV_ADAPTER_FT_FONT_STYLE_NORMAL, fb);
    g_fonts.text26 = load("inter", inter_ttf_start, inter_ttf_end, 27, ESP_LV_ADAPTER_FT_FONT_STYLE_BOLD, fb);
    g_fonts.text40 = load("inter", inter_ttf_start, inter_ttf_end, 42, ESP_LV_ADAPTER_FT_FONT_STYLE_BOLD, fb);
    g_fonts.clock = load("inter", inter_ttf_start, inter_ttf_end, 64, ESP_LV_ADAPTER_FT_FONT_STYLE_BOLD, fb);
    g_fonts.icon22 = load("mdi", mdi_ttf_start, mdi_ttf_end, 24, ESP_LV_ADAPTER_FT_FONT_STYLE_NORMAL, fb);
    g_fonts.icon34 = load("mdi", mdi_ttf_start, mdi_ttf_end, 36, ESP_LV_ADAPTER_FT_FONT_STYLE_NORMAL, fb);
    g_fonts.icon56 = load("mdi", mdi_ttf_start, mdi_ttf_end, 60, ESP_LV_ADAPTER_FT_FONT_STYLE_NORMAL, fb);
}

const char *mdi(uint32_t cp, char *b)
{
    // UTF-8 encode (MDI codepoints are in plane 15: 4 bytes)
    if (cp < 0x80) {
        b[0] = (char)cp; b[1] = 0;
    } else if (cp < 0x800) {
        b[0] = 0xC0 | (cp >> 6); b[1] = 0x80 | (cp & 0x3F); b[2] = 0;
    } else if (cp < 0x10000) {
        b[0] = 0xE0 | (cp >> 12); b[1] = 0x80 | ((cp >> 6) & 0x3F); b[2] = 0x80 | (cp & 0x3F); b[3] = 0;
    } else {
        b[0] = 0xF0 | (cp >> 18); b[1] = 0x80 | ((cp >> 12) & 0x3F);
        b[2] = 0x80 | ((cp >> 6) & 0x3F); b[3] = 0x80 | (cp & 0x3F); b[4] = 0;
    }
    return b;
}
