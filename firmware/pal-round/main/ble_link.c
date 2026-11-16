// BLE link: when Wi-Fi is gone, reach PAL through a bonded Android phone.
// The phone is a dumb TCP pipe to the gateway; TLS stays end-to-end. Protocol:
// firmware/pal-round/BLE_LINK.md.
//
//   esp_http_client / websocket ──TLS──► 127.0.0.1:443   (DNS hook below maps
//   the gateway host to loopback while the link is active)
//   bridge task: accepted sockets ⇄ stream mux frames over GATT
//
// The NimBLE stack runs only while it's needed (relay mode or a pairing
// window) and is torn down afterwards so its RAM returns to the heap.
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <strings.h>
#include <sys/time.h>
#include "app.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "esp_vfs_eventfd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "lwip/err.h"
#include "lwip/ip_addr.h"
#include "lwip/sockets.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "nvs.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "link";

void ble_store_config_init(void);

// --- protocol ------------------------------------------------------------------------

enum { F_HELLO = 1, F_OPEN, F_OPENED, F_DATA, F_CLOSE, F_CREDIT, F_MODE };
enum { R_NORMAL = 0, R_REFUSED, R_CONNECT, R_RESET, R_TIMEOUT };
#define PROTO_VERSION 1
#define MAX_STREAMS 6
#define WINDOW 32768
#define CREDIT_STEP 4096
#define PAIR_WINDOW_MS 120000
#define IDLE_AFTER_US 2000000

// 504c000X-0b1e-4f6c-9a3d-2b7e5a1c0000 (little-endian byte order)
#define PAL_UUID(x) BLE_UUID128_INIT(0x00, 0x00, 0x1c, 0x5a, 0x7e, 0x2b, 0x3d, 0x9a, 0x6c, 0x4f, 0x1e, 0x0b, x, 0x00, 0x4c, 0x50)
static const ble_uuid128_t SVC_UUID = PAL_UUID(0x01);
static const ble_uuid128_t TX_UUID = PAL_UUID(0x02);
static const ble_uuid128_t RX_UUID = PAL_UUID(0x03);

// --- state ---------------------------------------------------------------------------

typedef struct {
    int fd;                    // accepted local socket, -1 = free slot
    bool open;                 // OPENED received
    bool remote_closed;        // CLOSE received: flush rx, then close
    StreamBufferHandle_t rx;   // phone → device bytes (written by the host task)
    uint32_t tx_credit;        // bytes we may still send
    uint32_t rx_done;          // bytes written to fd, not yet credited back
    uint8_t pend[512];         // rx bytes taken from the buffer, not yet written to fd
    uint16_t pend_off, pend_len;
    uint8_t out[512];          // fd bytes read, not yet notified (notify ran out of buffers)
    uint16_t out_len;
} stream_t;

typedef struct { uint8_t type, sid; uint32_t val; uint64_t ts; } ctrl_t;
enum { C_HELLO = 100, C_DISCONNECTED, C_STOP };

static volatile bool s_running;       // NimBLE stack up
static volatile bool s_relay;         // relay mode (Wi-Fi off, waiting for / connected to the phone)
static volatile bool s_pairing;
static volatile bool s_up;            // HELLO exchanged
static volatile uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static volatile bool s_subscribed;
static uint16_t s_tx_handle;
static uint8_t s_own_addr;
static stream_t *s_st;                 // [MAX_STREAMS + 1], index = stream id (0 unused); PSRAM
static QueueHandle_t s_ctrl;
static int s_evfd = -1;
static TaskHandle_t s_bridge;
static SemaphoreHandle_t s_bridge_done;
static esp_timer_handle_t s_pair_timer;
static char s_gw_host[96];
static char s_name[32];
static bool s_busy;
static int64_t s_last_traffic;

static void advertise(void);

// --- the gateway host (DNS hook) ------------------------------------------------------

static void parse_gateway_host(void)
{
    const char *g = g_settings.gateway;
    const char *p = strstr(g, "://");
    p = p ? p + 3 : g;
    size_t n = strcspn(p, ":/");
    if (n >= sizeof(s_gw_host)) n = sizeof(s_gw_host) - 1;
    memcpy(s_gw_host, p, n);
    s_gw_host[n] = 0;
}

// lwIP calls this before DNS (CONFIG_LWIP_HOOK_NETCONN_EXT_RESOLVE_CUSTOM): while
// relaying, the gateway resolves to our loopback proxy.
int lwip_hook_netconn_external_resolve(const char *name, ip_addr_t *addr, u8_t addrtype, err_t *err)
{
    if (!s_relay || !s_gw_host[0] || !name || strcasecmp(name, s_gw_host) != 0) return 0;
    IP_ADDR4(addr, 127, 0, 0, 1);
    *err = ERR_OK;
    return 1;
}

// --- frames out ----------------------------------------------------------------------

static int frame_max(void)
{
    uint16_t mtu = s_conn != BLE_HS_CONN_HANDLE_NONE ? ble_att_mtu(s_conn) : 23;
    int m = (int)mtu - 3;
    return m > 512 ? 512 : m;
}

// One notification. False = not connected / no buffers right now (caller retries).
static bool tx_frame(uint8_t type, uint8_t sid, const void *p, size_t n)
{
    if (s_conn == BLE_HS_CONN_HANDLE_NONE || !s_subscribed || n + 2 > (size_t)frame_max()) return false;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(NULL, 0);
    if (!om) return false;
    uint8_t hdr[2] = {type, sid};
    if (os_mbuf_append(om, hdr, 2) || (n && os_mbuf_append(om, p, n))) {
        os_mbuf_free_chain(om);
        return false;
    }
    return ble_gatts_notify_custom(s_conn, s_tx_handle, om) == 0;   // consumes om
}

// Control frames are small and rare: retry for up to a second.
static bool tx_ctrl(uint8_t type, uint8_t sid, const void *p, size_t n)
{
    for (int i = 0; i < 50; i++) {
        if (tx_frame(type, sid, p, n)) return true;
        if (s_conn == BLE_HS_CONN_HANDLE_NONE) return false;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    ESP_LOGW(TAG, "control frame %u dropped", type);
    return false;
}

static void set_busy(bool busy)
{
    if (busy == s_busy || s_conn == BLE_HS_CONN_HANDLE_NONE) return;
    s_busy = busy;
    uint8_t m = busy;
    tx_ctrl(F_MODE, 0, &m, 1);
    // busy: 15–30 ms; idle: 300–500 ms, no latency (pushes from PAL arrive within half a second)
    struct ble_gap_upd_params p = {
        .itvl_min = busy ? 12 : 240, .itvl_max = busy ? 24 : 400, .latency = 0,
        .supervision_timeout = busy ? 400 : 600,
    };
    ble_gap_update_params(s_conn, &p);
}

// --- frames in (NimBLE host task) ---------------------------------------------------------

static void wake_bridge(void)
{
    if (s_evfd >= 0) {
        uint64_t one = 1;
        write(s_evfd, &one, sizeof(one));
    }
}

static void post_ctrl(uint8_t type, uint8_t sid, uint32_t val, uint64_t ts)
{
    ctrl_t c = {type, sid, val, ts};
    if (s_ctrl && xQueueSend(s_ctrl, &c, 0) != pdTRUE) ESP_LOGW(TAG, "control queue full");
    wake_bridge();
}

static void rx_frame(const uint8_t *b, size_t n)
{
    if (n < 2) return;
    uint8_t type = b[0], sid = b[1];
    const uint8_t *p = b + 2;
    n -= 2;
    if (type == F_HELLO) {
        if (n >= 9 && p[0] >= 1) {
            uint64_t ts = 0;
            for (int i = 7; i >= 0; i--) ts = ts << 8 | p[1 + i];
            post_ctrl(C_HELLO, 0, p[0], ts);
        }
        return;
    }
    if (sid < 1 || sid > MAX_STREAMS || !s_st) return;
    stream_t *s = &s_st[sid];
    switch (type) {
    case F_DATA:
        if (s->fd < 0 || !s->rx) return;
        if (xStreamBufferSend(s->rx, p, n, 0) != n) {
            ESP_LOGW(TAG, "stream %u: phone overran its window", sid);
            post_ctrl(F_CLOSE, sid, R_RESET, 0);
        }
        wake_bridge();
        break;
    case F_OPENED:
    case F_CLOSE:
        post_ctrl(type, sid, 0, 0);
        break;
    case F_CREDIT:
        if (n >= 4) post_ctrl(F_CREDIT, sid, p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24, 0);
        break;
    default:
        break;
    }
}

static int chr_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    static uint8_t buf[520];
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_READ_NOT_PERMITTED;
    uint16_t n = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof(buf), &n) != 0) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    rx_frame(buf, n);
    return 0;
}

static const struct ble_gatt_svc_def s_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {.uuid = &TX_UUID.u, .access_cb = chr_access, .flags = BLE_GATT_CHR_F_NOTIFY, .val_handle = &s_tx_handle},
            {.uuid = &RX_UUID.u, .access_cb = chr_access,
             .flags = BLE_GATT_CHR_F_WRITE_NO_RSP | BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC},
            {0},
        },
    },
    {0},
};

// --- GAP ---------------------------------------------------------------------------------

static void pairing_end(bool ok)
{
    if (!s_pairing) return;
    s_pairing = false;
    esp_timer_stop(s_pair_timer);
    app_post_str(EV_LINK, ok ? "paired" : "pairend");
}

static int gap_event(struct ble_gap_event *e, void *arg)
{
    struct ble_gap_conn_desc d;
    switch (e->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (e->connect.status == 0) {
            s_conn = e->connect.conn_handle;
            ESP_LOGI(TAG, "phone connected");
            ble_gap_security_initiate(s_conn);   // bonded: encrypt; new phone: pairing (only in the window)
        } else {
            advertise();
        }
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "phone disconnected (reason 0x%x)", e->disconnect.reason);
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        s_subscribed = false;
        s_busy = false;
        post_ctrl(C_DISCONNECTED, 0, 0, 0);
        advertise();
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (e->subscribe.attr_handle == s_tx_handle) s_subscribed = e->subscribe.cur_notify;
        break;
    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "mtu %u", e->mtu.value);
        break;
    case BLE_GAP_EVENT_PASSKEY_ACTION:
        if (e->passkey.params.action != BLE_SM_IOACT_DISP || !s_pairing) {
            ESP_LOGW(TAG, "pairing request outside the pairing window: refused");
            ble_gap_terminate(e->passkey.conn_handle, BLE_ERR_AUTH_FAIL);
            break;
        }
        {
            struct ble_sm_io io = {.action = BLE_SM_IOACT_DISP, .passkey = 100000 + esp_random() % 900000};
            ble_sm_inject_io(e->passkey.conn_handle, &io);
            char code[16];
            snprintf(code, sizeof(code), "c%06lu", (unsigned long)io.passkey);
            app_post_str(EV_LINK, code);
        }
        break;
    case BLE_GAP_EVENT_ENC_CHANGE:
        if (ble_gap_conn_find(e->enc_change.conn_handle, &d) != 0) break;
        if (e->enc_change.status == 0 && d.sec_state.bonded) {
            ESP_LOGI(TAG, "link encrypted (bonded)");
            if (s_pairing) pairing_end(true);
        } else {
            ESP_LOGW(TAG, "encryption failed (%d)", e->enc_change.status);
            if (s_pairing) app_post_str(EV_LINK, "pairfail");
            ble_gap_terminate(e->enc_change.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
        }
        break;
    case BLE_GAP_EVENT_REPEAT_PAIRING:
        // the phone forgot us and pairs again: only inside the pairing window
        if (!s_pairing) return BLE_GAP_REPEAT_PAIRING_IGNORE;
        if (ble_gap_conn_find(e->repeat_pairing.conn_handle, &d) == 0) ble_store_util_delete_peer(&d.peer_id_addr);
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        advertise();
        break;
    default:
        break;
    }
    return 0;
}

static void advertise(void)
{
    if (!s_running || s_conn != BLE_HS_CONN_HANDLE_NONE || (!s_relay && !s_pairing)) return;
    if (ble_gap_adv_active()) ble_gap_adv_stop();
    struct ble_hs_adv_fields f = {0};
    f.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    f.uuids128 = (ble_uuid128_t *)&SVC_UUID;
    f.num_uuids128 = 1;
    f.uuids128_is_complete = 1;
    ble_gap_adv_set_fields(&f);
    struct ble_hs_adv_fields r = {0};
    r.name = (uint8_t *)s_name;
    r.name_len = strlen(s_name);
    r.name_is_complete = 1;
    ble_gap_adv_rsp_set_fields(&r);
    // pairing: fast (the phone is scanning right now); relay: ~0.76 s (the
    // phone's background autoConnect finds it within seconds, ~0.1 mA)
    uint16_t itvl = s_pairing ? 160 : 1216;
    struct ble_gap_adv_params p = {
        .conn_mode = BLE_GAP_CONN_MODE_UND, .disc_mode = BLE_GAP_DISC_MODE_GEN, .itvl_min = itvl, .itvl_max = itvl + 32,
    };
    int rc = ble_gap_adv_start(s_own_addr, NULL, BLE_HS_FOREVER, &p, gap_event, NULL);
    if (rc) ESP_LOGW(TAG, "advertising failed: %d", rc);
}

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_own_addr);
    advertise();
}

static void on_reset(int reason) { ESP_LOGW(TAG, "host reset: %d", reason); }

static void host_task(void *arg)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

// --- the bridge (local sockets ⇄ frames) ----------------------------------------------

static void stream_free(int sid, bool send_close, uint8_t reason)
{
    stream_t *s = &s_st[sid];
    if (s->fd < 0) return;
    if (send_close) tx_ctrl(F_CLOSE, sid, &reason, 1);
    close(s->fd);
    s->fd = -1;
    s->open = s->remote_closed = false;
    s->pend_len = s->pend_off = s->out_len = 0;
    s->tx_credit = s->rx_done = 0;
    if (s->rx) xStreamBufferReset(s->rx);
}

static void close_all(bool send_close)
{
    for (int i = 1; i <= MAX_STREAMS; i++) stream_free(i, send_close, R_RESET);
}

static int open_listener(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(443), .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) || listen(fd, 4)) {
        ESP_LOGE(TAG, "loopback listener failed: %d", errno);
        close(fd);
        return -1;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    return fd;
}

static void handle_ctrl(const ctrl_t *c)
{
    stream_t *s = c->sid >= 1 && c->sid <= MAX_STREAMS ? &s_st[c->sid] : NULL;
    switch (c->type) {
    case C_HELLO: {
        if (!net_time_valid() && c->ts > 1700000000000ULL) {
            struct timeval tv = {.tv_sec = c->ts / 1000, .tv_usec = (c->ts % 1000) * 1000};
            settimeofday(&tv, NULL);
            ESP_LOGI(TAG, "clock set from the phone");
        }
        uint8_t h[2] = {PROTO_VERSION, MAX_STREAMS};
        tx_ctrl(F_HELLO, 0, h, sizeof(h));
        close_all(false);   // a fresh session: nothing from before survives
        if (!s_up) {
            s_up = true;
            ESP_LOGI(TAG, "link up (phone protocol v%lu)", (unsigned long)c->val);
            if (s_relay) app_post(EV_WIFI_UP);
        }
        break;
    }
    case C_DISCONNECTED:
        close_all(false);
        if (s_up) {
            s_up = false;
            ESP_LOGW(TAG, "link down");
            if (s_relay) app_post(EV_WIFI_DOWN);
        }
        break;
    case F_OPENED:
        if (s && s->fd >= 0) {
            s->open = true;
            s->tx_credit = WINDOW;
        }
        break;
    case F_CLOSE:
        if (s && s->fd >= 0) {
            if (c->val == R_RESET && !s->remote_closed) stream_free(c->sid, true, R_RESET);   // our side's fault
            else s->remote_closed = true;
        }
        break;
    case F_CREDIT:
        if (s && s->fd >= 0) s->tx_credit += c->val;
        break;
    }
}

static void accept_new(int lfd)
{
    while (true) {
        int fd = accept(lfd, NULL, NULL);
        if (fd < 0) return;
        int sid = 0;
        for (int i = 1; i <= MAX_STREAMS && !sid; i++)
            if (s_st[i].fd < 0) sid = i;
        if (!sid || !s_up) {
            close(fd);
            continue;
        }
        fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
        stream_t *s = &s_st[sid];
        s->fd = fd;
        uint8_t b[2 + sizeof(s_gw_host)];
        b[0] = 443 & 0xFF;
        b[1] = 443 >> 8;
        size_t hl = strlen(s_gw_host);
        memcpy(b + 2, s_gw_host, hl);
        if (!tx_ctrl(F_OPEN, sid, b, 2 + hl)) stream_free(sid, false, 0);
    }
}

// Move bytes for one stream. Returns true if anything moved.
static bool pump(int sid, bool readable)
{
    stream_t *s = &s_st[sid];
    bool moved = false;
    // phone → local socket
    while (true) {
        if (s->pend_off >= s->pend_len) {
            s->pend_off = 0;
            s->pend_len = xStreamBufferReceive(s->rx, s->pend, sizeof(s->pend), 0);
            if (!s->pend_len) break;
        }
        int w = send(s->fd, s->pend + s->pend_off, s->pend_len - s->pend_off, 0);
        if (w <= 0) {
            if (w < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
                stream_free(sid, true, R_RESET);
                return true;
            }
            break;
        }
        s->pend_off += w;
        s->rx_done += w;
        moved = true;
    }
    if (s->rx_done >= CREDIT_STEP) {
        uint32_t c = s->rx_done;
        uint8_t b[4] = {c, c >> 8, c >> 16, c >> 24};
        if (tx_ctrl(F_CREDIT, sid, b, 4)) s->rx_done = 0;
    }
    if (s->remote_closed && s->pend_off >= s->pend_len && !xStreamBufferBytesAvailable(s->rx)) {
        stream_free(sid, false, 0);
        return true;
    }
    // local socket → phone
    if (!s->open) return moved;
    if (s->out_len) {
        if (!tx_frame(F_DATA, sid, s->out, s->out_len)) return moved;
        s->tx_credit -= s->out_len;
        s->out_len = 0;
        moved = true;
    }
    while (readable && s->tx_credit > 0) {
        int room = frame_max() - 2;
        if (room > (int)sizeof(s->out)) room = sizeof(s->out);
        if ((uint32_t)room > s->tx_credit) room = s->tx_credit;
        int r = recv(s->fd, s->out, room, 0);
        if (r == 0) {
            stream_free(sid, true, R_NORMAL);
            return true;
        }
        if (r < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK) stream_free(sid, true, R_RESET);
            break;
        }
        moved = true;
        if (!tx_frame(F_DATA, sid, s->out, r)) {
            s->out_len = r;   // retry on the next pass
            break;
        }
        s->tx_credit -= r;
    }
    return moved;
}

static void bridge_task(void *arg)
{
    int lfd = open_listener();
    ctrl_t c;
    while (true) {
        while (xQueueReceive(s_ctrl, &c, 0) == pdTRUE) {
            if (c.type == C_STOP) goto out;
            handle_ctrl(&c);
        }
        fd_set rd, wr;
        FD_ZERO(&rd);
        FD_ZERO(&wr);
        int maxfd = s_evfd;
        FD_SET(s_evfd, &rd);
        if (lfd >= 0) {
            FD_SET(lfd, &rd);
            if (lfd > maxfd) maxfd = lfd;
        }
        bool retry = false, any_open = false;
        for (int i = 1; i <= MAX_STREAMS; i++) {
            stream_t *s = &s_st[i];
            if (s->fd < 0) continue;
            any_open = true;
            if (s->open && s->tx_credit > 0 && !s->out_len) FD_SET(s->fd, &rd);
            if (s->pend_off < s->pend_len) FD_SET(s->fd, &wr);
            if (s->out_len || (s->remote_closed) || xStreamBufferBytesAvailable(s->rx)) retry = true;
            if (s->fd > maxfd) maxfd = s->fd;
        }
        // block until something happens; short timeouts only while work is pending
        // or to drop to idle connection parameters after a quiet spell
        struct timeval tv = {0, 20000}, *tvp = &tv;
        if (!retry) {
            if (s_busy) {
                int64_t left = IDLE_AFTER_US - (esp_timer_get_time() - s_last_traffic);
                if (left < 0) left = 0;
                tv.tv_sec = left / 1000000;
                tv.tv_usec = left % 1000000;
            } else {
                tvp = NULL;
            }
        }
        int n = select(maxfd + 1, &rd, &wr, NULL, tvp);
        if (n < 0 && errno != EINTR) vTaskDelay(pdMS_TO_TICKS(50));
        if (n > 0 && FD_ISSET(s_evfd, &rd)) {
            uint64_t v;
            read(s_evfd, &v, sizeof(v));
        }
        if (n > 0 && lfd >= 0 && FD_ISSET(lfd, &rd)) accept_new(lfd);
        bool moved = false;
        for (int i = 1; i <= MAX_STREAMS; i++) {
            if (s_st[i].fd < 0) continue;
            moved |= pump(i, n > 0 && FD_ISSET(s_st[i].fd, &rd));
        }
        if (moved) {
            s_last_traffic = esp_timer_get_time();
            set_busy(true);
        } else if (s_busy && esp_timer_get_time() - s_last_traffic > IDLE_AFTER_US) {
            set_busy(false);
        }
        (void)any_open;
    }
out:
    close_all(s_conn != BLE_HS_CONN_HANDLE_NONE);
    if (lfd >= 0) close(lfd);
    xSemaphoreGive(s_bridge_done);
    vTaskDelete(NULL);
}

// --- start / stop -----------------------------------------------------------------------

static void pair_timeout(void *arg)
{
    if (!s_pairing) return;
    ESP_LOGI(TAG, "pairing window closed");
    pairing_end(false);
}

static bool stack_start(void)
{
    if (s_running) return true;
    snprintf(s_name, sizeof(s_name), "%s", pal_device_name());
    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble init failed: %s (internal free %u)", esp_err_to_name(err),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        return false;
    }
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_DISP_ONLY;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_gatts_count_cfg(s_svcs);
    ble_gatts_add_svcs(s_svcs);
    ble_svc_gap_device_name_set(s_name);
    ble_att_set_preferred_mtu(512);
    ble_store_config_init();
    s_running = true;
    nimble_port_freertos_init(host_task);
    ESP_LOGI(TAG, "bluetooth on (internal free %u)", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    return true;
}

static void stack_stop(void)
{
    if (!s_running) return;
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    ble_gap_adv_stop();
    nimble_port_stop();
    nimble_port_deinit();
    s_running = false;
    s_conn = BLE_HS_CONN_HANDLE_NONE;
    s_subscribed = false;
    ESP_LOGI(TAG, "bluetooth off (internal free %u)", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
}

static void common_init(void)
{
    static bool done;
    if (done) return;
    done = true;
    s_st = heap_caps_calloc(MAX_STREAMS + 1, sizeof(stream_t), MALLOC_CAP_SPIRAM);
    for (int i = 1; i <= MAX_STREAMS; i++) {
        s_st[i].fd = -1;
        s_st[i].rx = xStreamBufferCreateWithCaps(WINDOW, 1, MALLOC_CAP_SPIRAM);
    }
    s_ctrl = xQueueCreate(32, sizeof(ctrl_t));
    s_bridge_done = xSemaphoreCreateBinary();
    esp_vfs_eventfd_config_t ec = ESP_VFS_EVENTD_CONFIG_DEFAULT();
    esp_vfs_eventfd_register(&ec);
    s_evfd = eventfd(0, 0);
    const esp_timer_create_args_t ta = {.callback = pair_timeout, .name = "blepair"};
    esp_timer_create(&ta, &s_pair_timer);
}

bool link_start(void)
{
    common_init();
    if (s_relay) return true;
    parse_gateway_host();
    if (!s_gw_host[0]) {
        ESP_LOGW(TAG, "no gateway configured: nothing to relay to");
        return false;
    }
    if (!stack_start()) return false;
    xQueueReset(s_ctrl);
    s_relay = true;
    s_up = false;
    xTaskCreateWithCaps(bridge_task, "link", 6144, NULL, 5, &s_bridge, MALLOC_CAP_SPIRAM);
    advertise();
    ESP_LOGI(TAG, "relay mode: waiting for the phone (gateway %s)", s_gw_host);
    return true;
}

void link_stop(void)
{
    if (!s_relay) {
        if (!s_pairing) stack_stop();
        return;
    }
    post_ctrl(C_STOP, 0, 0, 0);
    xSemaphoreTake(s_bridge_done, pdMS_TO_TICKS(3000));
    bool was_up = s_up;
    s_relay = false;
    s_up = false;
    if (!s_pairing) stack_stop();
    else advertise();
    if (was_up) app_post(EV_WIFI_DOWN);
}

bool link_pair_start(void)
{
    common_init();
    if (!stack_start()) return false;
    s_pairing = true;
    esp_timer_stop(s_pair_timer);
    esp_timer_start_once(s_pair_timer, (uint64_t)PAIR_WINDOW_MS * 1000);
    if (s_conn == BLE_HS_CONN_HANDLE_NONE) advertise();
    ESP_LOGI(TAG, "pairing window open for %d s", PAIR_WINDOW_MS / 1000);
    return true;
}

// After a pairing window: drop the stack again unless we're relaying.
void link_pair_finish(void)
{
    if (s_pairing) pairing_end(false);
    if (!s_relay) stack_stop();
}

void link_forget(void)
{
    if (s_running) {
        ble_store_clear();
    } else {
        nvs_handle_t h;
        if (nvs_open("nimble_bond", NVS_READWRITE, &h) == ESP_OK) {
            nvs_erase_all(h);
            nvs_commit(h);
            nvs_close(h);
        }
    }
    g_settings.ble_bond = 0;
    settings_save();
}

bool link_active(void) { return s_relay; }
bool link_up(void) { return s_up; }
bool link_pairing(void) { return s_pairing; }

int link_streams(void)
{
    int n = 0;
    if (!s_st) return 0;
    for (int i = 1; i <= MAX_STREAMS; i++) n += s_st[i].fd >= 0;
    return n;
}

const char *link_state(void)
{
    if (!s_running) return "off";
    if (s_up) return "up";
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) return "connected";
    return s_pairing ? "pairing" : "advertising";
}
