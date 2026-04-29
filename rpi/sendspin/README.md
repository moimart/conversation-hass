# Sendspin sidecar

A [Sendspin](https://www.music-assistant.io/player-support/sendspin/) daemon
that registers the Pi as a Music Assistant player named
`${HAL_DEVICE_NAME} Speaker`. Audio coexists with HAL TTS through the same
PulseAudio socket; ducking happens in Pulse so HAL doesn't have to know about
MA at runtime.

## One-time host setup (PulseAudio role-ducking)

Add this line to `~/.config/pulse/default.pa` on the Pi (the user session
PulseAudio that the audio_streamer and sendspin containers share):

```
load-module module-role-ducking trigger_roles=phone ducking_roles=music volume=-25dB
```

Then `systemctl --user restart pulseaudio` (or reboot). After this:

- HAL TTS streams (tagged `media.role=phone` via `PULSE_PROP`) trigger ducking.
- Sendspin streams (tagged `media.role=music`) duck by 25 dB while HAL speaks
  and resume automatically.

## Channel mode (Stereo / Left only / Right only / Mono)

Channel layout is configured per-player in **Music Assistant**, not in the
daemon. The Anker PowerConf S330 is effectively mono — set:

> Music Assistant → Players → "*HAL Speaker*" → settings → **Channel Mode** = `Mono`

Other devices that pair into a stereo room would use `Left channel only` /
`Right channel only` on each daemon instance.

## Environment variables

| Var | Default | Notes |
| --- | --- | --- |
| `HAL_DEVICE_NAME` | `HAL` | Player advertises as `${HAL_DEVICE_NAME} Speaker` |
| `AUDIO_STREAMER_URL` | `http://localhost:${WEB_PORT}` | Where stream-start/stop hooks POST music state |
| `SENDSPIN_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Hardware volume buttons

The Anker speaker's volume buttons are read by the audio_streamer via evdev.
While Sendspin music is playing, the buttons forward to the AI server which
calls `media_player.volume_up` / `volume_down` on `SENDSPIN_PLAYER_ENTITY` (set
in the *server* `.env`). When music is idle, they adjust HAL's TTS volume as
before.

## Optional: explicit pause during TTS (Shape C fallback)

If the Pulse duck isn't deep enough, set `SENDSPIN_PAUSE_DURING_TTS=true` on
the server. HAL will then call `media_player.media_pause` on the configured
entity when it starts speaking and `media_player.media_play` when it returns
to idle. Leave it off and use Pulse ducking unless you find it lacking — the
explicit pause adds an MA round-trip per utterance and a perceptible gap.
