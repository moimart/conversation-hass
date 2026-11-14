// Fonts: rendered at runtime by FreeType from the TTFs embedded in the image
// (Inter for text, Material Design Icons for icons — the same icon set Home
// Assistant uses, so entity icons map 1:1).
#include "app.h"
#include "esp_log.h"

static const char *TAG = "fonts";
pal_fonts_t g_fonts;

extern const uint8_t inter_ttf_start[] asm("_binary_inter_ttf_start");
extern const uint8_t inter_ttf_end[] asm("_binary_inter_ttf_end");
extern const uint8_t mdi_ttf_start[] asm("_binary_mdi_ttf_start");
extern const uint8_t mdi_ttf_end[] asm("_binary_mdi_ttf_end");

static const lv_font_t *load(const char *name, const uint8_t *start, const uint8_t *end, int size,
                             int bold, const lv_font_t *fallback)
{
    // tiny_ttf renders from the TTF in flash on demand and caches the glyphs
    // (FreeType would need a 32 KB render stack in scarce internal RAM).
    (void)bold;
    lv_font_t *f = lv_tiny_ttf_create_data_ex(start, (size_t)(end - start), size, LV_FONT_KERNING_NONE, 96);
    if (!f) {
        ESP_LOGE(TAG, "font %s/%d failed", name, size);
        return fallback;
    }
    return f;
}

void fonts_init(void)
{
    const lv_font_t *fb = &lv_font_montserrat_14;
    if (!pal_lock()) return;
    g_fonts.text16 = load("inter", inter_ttf_start, inter_ttf_end, 17, 0, fb);
    g_fonts.text20 = load("inter", inter_ttf_start, inter_ttf_end, 21, 0, fb);
    g_fonts.text26 = load("inter", inter_ttf_start, inter_ttf_end, 27, 1, fb);
    g_fonts.text40 = load("inter", inter_ttf_start, inter_ttf_end, 42, 1, fb);
    g_fonts.clock = load("inter", inter_ttf_start, inter_ttf_end, 64, 1, fb);
    g_fonts.icon22 = load("mdi", mdi_ttf_start, mdi_ttf_end, 24, 0, fb);
    g_fonts.icon34 = load("mdi", mdi_ttf_start, mdi_ttf_end, 36, 0, fb);
    g_fonts.icon56 = load("mdi", mdi_ttf_start, mdi_ttf_end, 60, 0, fb);
    pal_unlock();
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
