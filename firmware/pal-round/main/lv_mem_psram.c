// LVGL's heap in PSRAM. With the C library allocator every small widget
// object landed in the ~200 KB of internal RAM that Wi-Fi and the display's
// DMA bounce buffers need; the UI is happy in PSRAM.
#include <stdlib.h>
#include "esp_heap_caps.h"
#include "lvgl.h"

void lv_mem_init(void) {}
void lv_mem_deinit(void) {}
lv_mem_pool_t lv_mem_add_pool(void *mem, size_t bytes) { (void)mem; (void)bytes; return NULL; }
void lv_mem_remove_pool(lv_mem_pool_t pool) { (void)pool; }

void *lv_malloc_core(size_t size)
{
    void *p = heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return p ? p : malloc(size);
}

void *lv_realloc_core(void *p, size_t new_size)
{
    void *n = heap_caps_realloc(p, new_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return n ? n : realloc(p, new_size);
}

void lv_free_core(void *p) { free(p); }

void lv_mem_monitor_core(lv_mem_monitor_t *mon_p) { (void)mon_p; }
lv_result_t lv_mem_test_core(void) { return LV_RESULT_OK; }
