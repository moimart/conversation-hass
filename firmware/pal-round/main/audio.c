// Audio: ES7210 mic + ES8311 speaker on one full-duplex I2S port, both at
// 16 kHz / 16-bit mono (the server resamples TTS to match, ?rate=16000).
//
// Recording runs in its own task into a PSRAM buffer with a simple energy VAD
// (adaptive noise floor): it ends ~0.9 s after speech stops, after 6 s with no
// speech, at 15 s, or when the caller stops it (button release / second tap).
// Playback takes a whole WAV (TTS replies are short) and streams it out in
// chunks so it can be interrupted.
//
// Power: both codecs (and the speaker amplifier, via the ES8311's PA pin) are
// only open while in use. The mic opens per recording; the speaker opens for a
// playback and closes 1.5 s after the queue runs dry. A closed I2S channel also
// releases its PM lock, so the chip can light-sleep.
#include <math.h>
#include <string.h>
#include "app.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "audio";

#define FRAME 320                         // 20 ms
#define REC_MAX_S 15
#define REC_MAX (PAL_SAMPLE_RATE * REC_MAX_S)

static esp_codec_dev_handle_t s_spk, s_mic;
static int16_t *s_rec;
static volatile size_t s_rec_n;
static volatile bool s_rec_on, s_rec_stop, s_rec_vad;
static volatile bool s_play_on, s_play_stop;
static volatile float s_level;
static TaskHandle_t s_rec_task;
static QueueHandle_t s_play_q;
static SemaphoreHandle_t s_dev_lock;
static bool s_spk_open;
static int s_volume = 70;

static const esp_codec_dev_sample_info_t FS = {
    .sample_rate = PAL_SAMPLE_RATE, .channel = 1, .bits_per_sample = 16,
};

// open/close the two halves of the shared full-duplex port one at a time
static bool dev_open(esp_codec_dev_handle_t dev)
{
    xSemaphoreTake(s_dev_lock, portMAX_DELAY);
    esp_codec_dev_sample_info_t fs = FS;
    bool ok = esp_codec_dev_open(dev, &fs) == ESP_CODEC_DEV_OK;
    if (ok && dev == s_mic) esp_codec_dev_set_in_gain(s_mic, 30.0f);
    if (ok && dev == s_spk) esp_codec_dev_set_out_vol(s_spk, s_volume);
    xSemaphoreGive(s_dev_lock);
    if (!ok) ESP_LOGE(TAG, "%s open failed", dev == s_mic ? "mic" : "speaker");
    return ok;
}

static void dev_close(esp_codec_dev_handle_t dev)
{
    xSemaphoreTake(s_dev_lock, portMAX_DELAY);
    esp_codec_dev_close(dev);
    bool other_open = dev == s_mic ? s_spk_open : s_rec_on;
    if (!other_open) bsp_audio_idle();
    xSemaphoreGive(s_dev_lock);
}

typedef struct { uint8_t *buf; size_t len; bool notify; } play_job_t;

static float rms(const int16_t *s, int n)
{
    double acc = 0;
    for (int i = 0; i < n; i++) acc += (double)s[i] * s[i];
    return sqrtf((float)(acc / n));
}

// --- recording ---------------------------------------------------------------

static void rec_task(void *arg)
{
    int16_t frame[FRAME];
    while (true) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        s_rec_n = 0;
        if (!dev_open(s_mic)) {
            s_rec_on = false;
            app_post(EV_REC_DONE);
            continue;
        }
        float noise = 0;
        int noise_frames = 0, silence_ms = 0, total_ms = 0;
        bool speech = false;
        while (!s_rec_stop && s_rec_n + FRAME <= REC_MAX) {
            if (esp_codec_dev_read(s_mic, frame, sizeof(frame)) != ESP_CODEC_DEV_OK) {
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
            memcpy(s_rec + s_rec_n, frame, sizeof(frame));
            s_rec_n += FRAME;
            total_ms += 20;
            float r = rms(frame, FRAME);
            s_level = fminf(1.0f, r / 6000.0f);
            if (total_ms <= 300) {           // first 300 ms: learn the noise floor
                noise += r;
                noise_frames++;
                continue;
            }
            float floor = noise_frames ? noise / noise_frames : 200;
            float thresh = fmaxf(floor * 3.0f, 450.0f);
            if (r > thresh) {
                speech = true;
                silence_ms = 0;
            } else {
                silence_ms += 20;
            }
            if (s_rec_vad) {
                if (speech && silence_ms >= 900) break;
                if (!speech && total_ms >= 6000) { s_rec_n = 0; break; }
            }
        }
        dev_close(s_mic);
        if (!speech && s_rec_vad) s_rec_n = 0;
        ESP_LOGI(TAG, "recorded %u samples (speech=%d)", (unsigned)s_rec_n, speech);
        s_level = 0;
        s_rec_on = false;
        app_post(EV_REC_DONE);
    }
}

bool audio_record_start(bool vad)
{
    if (s_rec_on || !s_mic) return false;
    audio_stop_playback();
    s_rec_vad = vad;
    s_rec_stop = false;
    s_rec_on = true;
    xTaskNotifyGive(s_rec_task);
    return true;
}

void audio_record_stop(void) { s_rec_stop = true; }
bool audio_recording(void) { return s_rec_on; }

int16_t *audio_recording_data(size_t *samples)
{
    *samples = s_rec_n;
    return s_rec;
}

float audio_level(void) { return s_level; }

// --- playback ----------------------------------------------------------------

static bool wav_pcm(const uint8_t *w, size_t len, const uint8_t **pcm, size_t *pcm_len, int *rate, int *ch)
{
    if (len < 44 || memcmp(w, "RIFF", 4) || memcmp(w + 8, "WAVE", 4)) return false;
    size_t p = 12;
    *rate = PAL_SAMPLE_RATE;
    *ch = 1;
    while (p + 8 <= len) {
        uint32_t sz = w[p + 4] | w[p + 5] << 8 | w[p + 6] << 16 | (uint32_t)w[p + 7] << 24;
        if (!memcmp(w + p, "fmt ", 4) && p + 24 <= len) {
            *ch = w[p + 10] | w[p + 11] << 8;
            *rate = w[p + 12] | w[p + 13] << 8 | w[p + 14] << 16 | (uint32_t)w[p + 15] << 24;
        } else if (!memcmp(w + p, "data", 4)) {
            *pcm = w + p + 8;
            *pcm_len = (p + 8 + sz <= len) ? sz : len - p - 8;
            return true;
        }
        p += 8 + sz + (sz & 1);
    }
    return false;
}

static void play_task(void *arg)
{
    play_job_t job;
    while (true) {
        if (xQueueReceive(s_play_q, &job, s_spk_open ? pdMS_TO_TICKS(1500) : portMAX_DELAY) != pdTRUE) {
            dev_close(s_spk);           // idle: codec + amplifier off
            s_spk_open = false;
            continue;
        }
        if (!s_spk_open) s_spk_open = dev_open(s_spk);
        const uint8_t *pcm;
        size_t n;
        int rate, ch;
        s_play_on = true;
        s_play_stop = false;
        if (s_spk_open && wav_pcm(job.buf, job.len, &pcm, &n, &rate, &ch)) {
            if (rate != PAL_SAMPLE_RATE || ch != 1) {
                ESP_LOGW(TAG, "wav %d Hz x%d (expected 16 kHz mono) — playing anyway", rate, ch);
            }
            size_t off = 0;
            const size_t chunk = 1024;
            while (off < n && !s_play_stop) {
                size_t c = (n - off) < chunk ? (n - off) : chunk;
                c &= ~1u;
                if (!c) break;
                s_level = fminf(1.0f, rms((const int16_t *)(pcm + off), c / 2) / 5000.0f);
                esp_codec_dev_write(s_spk, (void *)(pcm + off), c);
                off += c;
            }
            // flush the DMA tail with silence so the last syllable isn't cut
            static int16_t zeros[FRAME];
            for (int i = 0; i < 6 && !s_play_stop; i++) esp_codec_dev_write(s_spk, zeros, sizeof(zeros));
        } else {
            ESP_LOGW(TAG, "not a PCM wav (%u bytes)", (unsigned)job.len);
        }
        free(job.buf);
        s_level = 0;
        s_play_on = uxQueueMessagesWaiting(s_play_q) > 0;
        if (job.notify) app_post(EV_PLAY_DONE);
    }
}

static bool enqueue(uint8_t *buf, size_t len, bool notify)
{
    play_job_t job = {buf, len, notify};
    if (!s_spk || xQueueSend(s_play_q, &job, 0) != pdTRUE) {
        free(buf);
        return false;
    }
    s_play_on = true;
    return true;
}

bool audio_play_wav(uint8_t *wav, size_t len) { return enqueue(wav, len, true); }

void audio_stop_playback(void)
{
    play_job_t job;
    while (s_play_q && xQueueReceive(s_play_q, &job, 0) == pdTRUE) {
        free(job.buf);
        if (job.notify) app_post(EV_PLAY_DONE);
    }
    s_play_stop = true;
}

bool audio_playing(void) { return s_play_on; }

void audio_beep(int kind)
{
    // tiny two-tone earcons, synthesized as a WAV
    static const struct { int f1, f2, ms; } tones[] = {{880, 1320, 70}, {1320, 880, 70}, {330, 220, 120}};
    int t = kind < 0 || kind > 2 ? 0 : kind;
    int per = PAL_SAMPLE_RATE * tones[t].ms / 1000;
    size_t n = per * 2, bytes = 44 + n * 2;
    uint8_t *w = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM);
    if (!w) return;
    memset(w, 0, 44);
    memcpy(w, "RIFF", 4);
    memcpy(w + 8, "WAVEfmt ", 8);
    w[16] = 16; w[20] = 1; w[22] = 1;
    w[24] = PAL_SAMPLE_RATE & 0xFF; w[25] = PAL_SAMPLE_RATE >> 8;
    w[34] = 16;
    memcpy(w + 36, "data", 4);
    uint32_t dl = n * 2;
    memcpy(w + 40, &dl, 4);
    int16_t *s = (int16_t *)(w + 44);
    for (size_t i = 0; i < n; i++) {
        int f = i < (size_t)per ? tones[t].f1 : tones[t].f2;
        float env = sinf((float)M_PI * (float)(i % per) / per);
        s[i] = (int16_t)(sinf(2 * (float)M_PI * f * i / PAL_SAMPLE_RATE) * 9000 * env);
    }
    enqueue(w, bytes, false);
}

void audio_set_volume(int vol)
{
    s_volume = vol < 0 ? 0 : (vol > 100 ? 100 : vol);
    if (s_spk && s_spk_open) esp_codec_dev_set_out_vol(s_spk, s_volume);
}

void audio_init(void)
{
    s_dev_lock = xSemaphoreCreateMutex();
    // opening/closing channels that are already in that state logs a harmless
    // "channel has not been enabled yet" error on every power-down
    esp_log_level_set("i2s_common", ESP_LOG_NONE);
    s_rec = heap_caps_malloc(REC_MAX * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    s_play_q = xQueueCreate(4, sizeof(play_job_t));
    s_spk = bsp_audio_codec_speaker_init();
    s_mic = bsp_audio_codec_microphone_init();
    // probe both once, then leave them closed until needed
    if (s_spk && !dev_open(s_spk)) s_spk = NULL;
    if (s_spk) dev_close(s_spk);
    if (s_mic && !dev_open(s_mic)) s_mic = NULL;
    if (s_mic) dev_close(s_mic);
    audio_set_volume(g_settings.volume);
    xTaskCreatePinnedToCoreWithCaps(rec_task, "rec", 4096, NULL, 6, &s_rec_task, 1, MALLOC_CAP_SPIRAM);
    xTaskCreatePinnedToCoreWithCaps(play_task, "play", 4096, NULL, 6, NULL, 1, MALLOC_CAP_SPIRAM);
    ESP_LOGI(TAG, "audio ready (spk=%d mic=%d)", s_spk != NULL, s_mic != NULL);
}
