# PAL Round ⇄ phone BLE link

When the PAL Round has no Wi-Fi (main and backup both unreachable) it can reach
PAL through a bonded Android phone over Bluetooth LE. The phone is a **dumb TCP
pipe to the PAL gateway only**: the device still does its own TLS to
`pal.martinez.sh`, so the phone only ever carries ciphertext and never sees the
device's pairing token.

```
 device: esp_http_client / websocket ──TLS──► 127.0.0.1:443 (local proxy)
           (DNS hook: gateway host → 127.0.0.1 while the link is up)
                              │ BLE GATT frames (stream mux)
 phone:  PAL app foreground service ── TCP ──► <gateway host>:443
```

## GATT

| | UUID | Properties |
|---|---|---|
| Service | `504c0001-0b1e-4f6c-9a3d-2b7e5a1c0000` | advertised (128-bit UUID in the advertising data, the name in the scan response) |
| TX (device → phone) | `504c0002-0b1e-4f6c-9a3d-2b7e5a1c0000` | notify, encrypted |
| RX (phone → device) | `504c0003-0b1e-4f6c-9a3d-2b7e5a1c0000` | write without response, encrypted |

Both characteristics need an encrypted (bonded) link, so touching them from an
unbonded phone triggers pairing. The phone requests MTU 517 and the 2M PHY after
connecting. **One frame = one ATT write / notification** (never split); the
largest frame is `MTU - 3` bytes.

## Pairing

LE Secure Connections, MITM, bonding. The device is **DisplayOnly**: it shows a
6-digit passkey on its screen and the phone's system dialog asks for it. The
device accepts *new* pairings only while it's in pairing mode (Settings → "Pair a
phone (Bluetooth)", 2 minutes); outside it, a passkey request drops the
connection. Bonds are stored on both sides.

Device name in advertising: `PAL Round XXXX` (same suffix as its PAL device name).

**Starting a pairing**: from the phone app (Settings → PAL Round → Pair) the app
calls `POST /api/satellite/ble_pair` (Bearer); PAL pushes `{"type":"ble_pair",
"from": <phone name>}` over the WebSocket to every online PAL Round, which opens
its window and shows "Pair a phone". Or on the Round: Settings → Device → Phone.
A button press on the Round cancels the window.

## Frames

`[type u8][stream u8][payload…]`, little-endian integers. Stream 0 = control.

| Type | Name | Direction | Payload |
|---|---|---|---|
| `0x01` | HELLO | phone → device | `u8 version (=1)`, `u64 unix time ms` |
| `0x01` | HELLO | device → phone | `u8 version (=1)`, `u8 max streams` |
| `0x02` | OPEN | device → phone | `u16 port`, host (UTF-8, rest of frame). Stream id 1…max |
| `0x03` | OPENED | phone → device | — (TCP connected) |
| `0x04` | DATA | both | bytes |
| `0x05` | CLOSE | both | optional `u8 reason` (0 normal, 1 refused/not allowed, 2 connect failed, 3 reset, 4 timeout) |
| `0x06` | CREDIT | both | `u32 bytes` — more room granted to the other side |
| `0x07` | MODE | device → phone | `u8 0 = idle, 1 = busy` — connection priority hint |

* The phone sends HELLO right after it subscribes to TX; the device answers with
  its own HELLO. The link counts as **up** on the device only after that
  exchange (the device also sets its clock from the phone's time if it has none).
* **OPEN**: the phone accepts only `port == 443` and a host equal to the gateway
  host the app is paired with; anything else gets `CLOSE reason 1`. On TCP
  connect it answers OPENED; on failure `CLOSE reason 2`.
* **Flow control**: each direction of each stream starts with a window of
  **32768 bytes** once OPENED is sent/received. A sender never has more DATA
  bytes outstanding than the credit the receiver granted; the receiver sends
  CREDIT as it drains (in chunks of ≥ 4096 bytes).
* **CLOSE** is a full close of both directions. Either side sends it once; the
  other side closes its socket and does not answer with another CLOSE.
  Data queued before a CLOSE is delivered first.
* If the BLE connection drops, every stream is closed on both sides.
* **MODE busy** while any stream is open: the phone switches to
  `CONNECTION_PRIORITY_HIGH`; idle (no streams for ~2 s) → `LOW_POWER`. The
  device also requests matching connection parameters itself.

## Edge cases (both sides implement these)

* Any TCP connect failure (DNS, timeout, refused) → `CLOSE 2` (4 is unused).
* DATA before OPENED or beyond the window → the receiver sends `CLOSE 3` for
  that stream. DATA/CREDIT for an unknown (already closed) stream is dropped.
* The phone only changes connection priority on MODE frames; the device sends
  `MODE 0` after ~2 s without traffic (the WebSocket stream stays open, so
  "no streams" would never happen).

## Device RAM

The controller runs from flash (`BT_CTRL_RUN_IN_FLASH_ONLY`), costing ~10 KB of
static internal RAM instead of ~27 KB; the NimBLE host allocates from PSRAM and
the whole stack is initialised only while relaying or pairing. Flash erases
can stall BLE briefly, so relay mode avoids flash writes (orbs come from the
FAT cache; a not-yet-cached orb would still download).

## Power

* The link runs only when Wi-Fi has failed on every configured network; Wi-Fi
  is stopped while it's up (no radio sharing), and every 10 minutes the device
  scans for its networks and leaves BLE mode when one is back.
* Idle: connection interval ~ 500–1000 ms with peripheral latency; busy:
  15–30 ms. Advertising (waiting for the phone) every ~1 s.
* The orb plays from flash; nothing is streamed over the link but HTTP/WS.
* Android: the relay is a foreground service (`connectedDevice`), reconnecting
  with `autoConnect=true` (the OS's low-duty background scan), no app scanning
  except while pairing.
