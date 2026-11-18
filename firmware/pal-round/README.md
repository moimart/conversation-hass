# PAL Round

A native PAL satellite for the **Waveshare ESP32-S3-Touch-AMOLED-1.75C**
(466×466 round AMOLED, capacitive touch, ES8311 speaker codec, ES7210 dual mic,
AXP2101 PMU with battery, BOOT + PWR buttons).

## What it does

| Screen | |
|---|---|
| **Home** | The orb (~96% of the screen) with what you said and PAL's reply in a box over it. Talking is buttons-only: touching the orb never listens; holding it opens Settings |
| **Swipe left** | Clock, date and weather, then Home Assistant pages: the dashboard you pick in Settings, as native round tiles (tap = toggle / run; long-press a light or cover = brightness / position dial) and full-page climate, media and weather controls |
| **Swipe right** (from the orb) | How-to page: talk, voice notes, settings — and the **button lock**: while locked, BOOT and PWR do nothing (no talk, no power-off; a lock icon shows under the orb), so PAL can't listen at all. Touch keeps working, so swipe back here to unlock. Saved across reboots |
| **Swipe up** | Settings: volume, brightness, screen timeout, HA dashboard, orb, firmware update, Wi-Fi setup, re-pair |

**Talking to PAL:** press **BOOT** (or PWR) and speak — touching the orb never listens. Recording stops by
itself when you pause; holding BOOT records until you let go. The audio goes to
PAL's speech-to-text (`/api/satellite/stt`), the text to `/api/command`, and the
reply comes back over the device's `/ws/ui` socket: state, text, and the spoken
reply (`tts_play` → `/api/satellite/tts?rate=16000`). Announcements, finished
timers and the per-device voice all work as on the phones.

**Voice notes:** double-press a button (BOOT or PWR). Two
blips, and the box shows "● Recording note 0:12". Talk as long as you like (10 min
cap); the audio streams to `POST /api/satellite/note` while you speak. Press once
more to finish: PAL transcribes it and stores it in the Notes list (the Notes
view on the hub/phones, and the `list_notes` MCP tool).

**PWR button:** short press = wake the screen / talk, long press = power off.

**The orb** shows the hub's orb, or this device's own pick from Settings. The server
re-renders the theme's clips or a custom orb as `.palm` files: 448 px circular
JPEG frames at 12 fps. Frames are decoded on core 0 and written straight to the
panel (LVGL only redraws the bands under the text box/status), which is what
keeps a steady 12 fps at this size. The device caches them in flash and decodes them with
`esp_new_jpeg`. Themes without orb media get a native LVGL orb in the theme's
accent colour.

## Server side

`server/app/device_api.py` + `routes_device.py` (`/api/device/{info,orb,pages,firmware}`),
all gateway-allowlisted, so the device also works away from home via the
gateway URL it received at pairing.

## Build & flash

Needs only podman or docker. It uses Espressif's `espressif/idf:v5.5.2` image:

```bash
./build.sh                    # build
./build.sh flash /dev/ttyACM0 # flash over the USB-C port
```

## First setup

**Without a computer:** on first boot the device opens a Wi-Fi network called
`PAL-Round-XXXX` and shows a QR code to join it. Your phone pops a setup page
(or open `192.168.4.1`). Pick your Wi-Fi network and enter its password; the PAL
server is found automatically (mDNS `_pal._tcp`). Then type the 6-digit code the
hub shows into the round keypad.

**Over USB (serial console, 115200):**

```
wifi "My Network" password
wifi2 "Backup Network" password    # optional backup network (wifi2 - removes it)
server http://10.20.30.185:8765     # optional (otherwise discovered)
status
```

**Backup Wi-Fi:** after 4 failed attempts on one network the device switches to
the other; while on the backup it scans for the main network every 10 minutes and
moves back when it's in range. Set it on the console (`wifi2`) or in the setup
portal. `net main|backup|check` forces a switch / the check (for testing).

## OTA updates

Put a build on the server and the device offers it under Settings → Update:

```bash
mkdir -p server/runtime/firmware/pal-round
cp build/pal-round.bin server/runtime/firmware/pal-round/firmware.bin
echo 0.1.1 > server/runtime/firmware/pal-round/version   # = CONFIG_APP_PROJECT_VER
```

## Notes

- **Fonts:** Inter for text and Material Design Icons (the set Home Assistant uses)
  are embedded as TTFs and rendered with tiny_ttf at runtime. The icon names the
  server may send are in `tools/icon_names.txt`. Run `tools/gen_icons.py` to
  regenerate `main/icons.h` + `server/app/device_icons.py`.
- **Audio:** everything runs at 16 kHz mono 16-bit on one full-duplex I2S port.
- **Power:** dynamic frequency scaling (240 ↔ 40 MHz) with automatic light sleep;
  Wi-Fi modem sleep keeps the PAL socket up (every DTIM beacon with the screen on,
  every 3rd with it off). Screen off = the panel in deep standby, LVGL not drawing,
  the orb stopped; the codecs and speaker amp are powered only while recording or
  playing; the orb only decodes on the home page. LVGL's clock comes from
  `esp_timer` (no 1 ms tick timer). Light sleep is held off while a USB host is
  attached (so the console works). Console `pm` / `top` show lock and per-task
  CPU time; `beep` / `rec` test the speaker / mic.
