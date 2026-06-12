#!/usr/bin/env python3
"""Host-side watchdog for the Wyoming TTS service (wyoming-omnivoice).

The OmniVoice container occasionally wedges: it still accepts connections and the
model reports "Ready", but every `synthesize` hangs and returns no audio — the
ai-server logs "Wyoming TTS response timeout" / "TTS returned no audio" after a
30s wait, and every spoken response is silent (the orb sits in the speaking
animation, text only lands when the turn times out). Only a container restart
clears it.

This probes the service with a tiny REAL synth on an interval and
`docker restart`s the container after N consecutive failures, waiting for it to
reload before probing again — so the hang auto-recovers instead of needing a
manual restart.

Runs as a systemd --user service on the AI-server host (10.20.30.185), like the
openclaw-gateway. Pure stdlib; no third-party deps. The moimart user is in the
docker group there, so `docker restart` works without sudo.
"""
import asyncio
import json
import os
import subprocess
import time

HOST = os.environ.get("TTS_WATCHDOG_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_WATCHDOG_PORT", "10300"))
CONTAINER = os.environ.get("TTS_WATCHDOG_CONTAINER", "wyoming-omnivoice")
INTERVAL = float(os.environ.get("TTS_WATCHDOG_INTERVAL", "90"))        # s between probes
PROBE_TIMEOUT = float(os.environ.get("TTS_WATCHDOG_PROBE_TIMEOUT", "15"))
FAILS_BEFORE_RESTART = int(os.environ.get("TTS_WATCHDOG_FAILS", "2"))  # consecutive
RESTART_GRACE = float(os.environ.get("TTS_WATCHDOG_GRACE", "45"))      # s after restart


def log(msg: str) -> None:
    print(f"[tts-watchdog] {msg}", flush=True)


async def _send(writer, etype, data=None):
    db = json.dumps(data).encode() if data else b""
    header = {"type": etype, "data_length": len(db), "payload_length": 0}
    writer.write((json.dumps(header) + "\n").encode())
    if db:
        writer.write(db)
    await writer.drain()


async def probe() -> bool:
    """True iff a tiny synth returns an audio event within PROBE_TIMEOUT."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, PORT), timeout=5)
    except Exception as e:
        log(f"probe: connect failed: {e}")
        return False
    try:
        await _send(writer, "synthesize", {"text": "ok"})
        deadline = time.monotonic() + PROBE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if not line:
                return False
            try:
                header = json.loads(line.decode().strip())
            except Exception:
                continue
            # Drain any data/payload that follow this event's header.
            dl = int(header.get("data_length", 0))
            pl = int(header.get("payload_length", 0))
            if dl:
                await reader.readexactly(dl)
            if pl:
                await reader.readexactly(pl)
            if str(header.get("type", "")).startswith("audio"):
                return True   # got audio → service is healthy
    except Exception as e:
        log(f"probe: error: {e}")
        return False
    finally:
        try:
            writer.close()
        except Exception:
            pass


def restart() -> None:
    log(f"restarting {CONTAINER} …")
    try:
        subprocess.run(["docker", "restart", "-t", "5", CONTAINER],
                       check=True, timeout=90, capture_output=True)
        log("restart issued")
    except Exception as e:
        log(f"restart failed: {e}")


async def main() -> None:
    log(f"started — probing {HOST}:{PORT} every {INTERVAL:.0f}s "
        f"(restart after {FAILS_BEFORE_RESTART} consecutive failures)")
    fails = 0
    while True:
        ok = await probe()
        if ok:
            if fails:
                log("recovered (probe ok)")
            fails = 0
        else:
            fails += 1
            log(f"probe FAILED ({fails}/{FAILS_BEFORE_RESTART})")
            if fails >= FAILS_BEFORE_RESTART:
                restart()
                fails = 0
                await asyncio.sleep(RESTART_GRACE)  # let the model reload
                continue
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
