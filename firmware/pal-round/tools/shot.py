#!/usr/bin/python3
"""Screenshot the PAL Round over USB (console `shot`) into a round PNG.

    /usr/bin/python3 tools/shot.py out.png                      # as it is now
    /usr/bin/python3 tools/shot.py out.png "ui page 0"          # run console commands first
    /usr/bin/python3 tools/shot.py out.png "ui settings 1" --wait 1.5

The device prints the screen and the top layer (settings, toasts, overlays) as
base64 ARGB8888; they're composited here and masked to the round panel. The
orb's video frames go straight to the panel, so they don't show up (its LVGL
fallback does). Needs pyserial + Pillow (system python).
"""
import argparse
import base64
import sys
import time

import serial
from PIL import Image, ImageDraw


def read_layers(port, timeout):
    layers, cur, buf = {}, None, []
    end = time.time() + timeout
    while time.time() < end:
        line = port.readline().decode("ascii", "replace").strip()
        if not line:
            continue
        if line.startswith("SHOT-ERR"):
            sys.exit(f"device: {line}")
        if line.startswith("SHOT "):
            _, name, w, h, stride = line.split()
            cur, buf = (name, int(w), int(h), int(stride)), []
        elif line == "END" and cur:
            name, w, h, stride = cur
            raw = base64.b64decode("".join(buf))
            rows = b"".join(raw[y * stride:y * stride + w * 4] for y in range(h))
            # LVGL ARGB8888 is little-endian B,G,R,A in memory
            layers[name] = Image.frombytes("RGBA", (w, h), rows, "raw", "BGRA")
            cur = None
            if len(layers) == 2:
                return layers
        elif cur:
            buf.append(line)
    sys.exit(f"timed out (got {list(layers)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("commands", nargs="*", help="console commands to run first")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--wait", type=float, default=0.8, help="seconds after the commands")
    a = ap.parse_args()
    with serial.Serial(a.port, 115200, timeout=1) as p:
        for c in a.commands:
            p.write((c + "\n").encode())
            time.sleep(0.3)
        time.sleep(a.wait)
        p.reset_input_buffer()
        p.write(b"shot\n")
        layers = read_layers(p, 60)
    img = Image.new("RGBA", layers["screen"].size, (0, 0, 0, 255))
    img.alpha_composite(layers["screen"])
    img.alpha_composite(layers["top"])
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w - 1, h - 1), fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    out.save(a.out)
    print(a.out)


if __name__ == "__main__":
    main()
