# Writing PAL themes

PAL's kiosk UI is plug-in themable. A theme is a folder of files; drop it in, the server picks it up within ~10 seconds, and the kiosk's picker updates without a restart on either side. This document covers the contract, the available CSS variables, the optional effect.js API, and how to develop and ship a theme.

---

## TL;DR

1. Pick a name like `myth` (lowercase, kebab-case).
2. Create `server/themes/myth/manifest.json` and `server/themes/myth/theme.css`.
3. The server's theme registry picks it up on the next polling tick (default 10 s) and pushes a `themes_changed` event. The kiosk's dropdown updates; selecting your theme lazy-loads its CSS.
4. Optional: add `effect.js` for an animated background.

Zero code edits, zero rebuilds.

---

## File layout

```
server/themes/<name>/
├── manifest.json          # required
├── theme.css              # required
└── effect.js              # optional, ES module
```

The directory name is **authoritative** for the theme's identifier. The `name` field in `manifest.json` should match; if it differs, the directory name wins and a warning is logged.

Themes live on the AI-server host at `./server/themes/` and are mounted into the container at `/app/themes`. The mount is read-write so themes can be installed at runtime (a future "theme installer" could write into this volume).

---

## `manifest.json`

```json
{
    "name": "myth",
    "display_name": "Myth — Olive on slate",
    "description": "Soft olive accents on weathered slate-grey.",
    "version": "1.0.0",
    "kind": "dark",
    "effect": "effect.js"
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | yes | string | Must equal the directory name (lowercase, kebab-case is conventional). Used as the CSS selector `body.theme-<name>` and as the value in MQTT/voice/HA. |
| `display_name` | yes | string | Shown in the kiosk picker and HA select. Free form (Unicode, em-dashes, etc.). |
| `description` | recommended | string | One-line summary. Surfaced in `/api/themes`; useful for theme-browser UIs. |
| `version` | recommended | string | Semantic-ish version; informational. |
| `kind` | yes | `"dark"` or `"light"` | Controls grouping in HA selects (darks first, lights second). Defaults to `dark`. |
| `effect` | no | string | Filename (relative to the theme folder) of an ES-module effect — usually `effect.js`. Omit for colors-only themes. |

Unknown keys are preserved silently — fine for forward-compat extensions.

---

## `theme.css`

A single CSS block scoped to `body.theme-<name>` that overrides any subset of the kiosk's CSS custom properties.

Minimum viable theme — change just the accent:

```css
body.theme-myth {
    --accent: #88b04b;
    --accent-glow: #b5d381;
    --accent-deep: #4d6627;
}
```

That's enough to recolor the orb's iris, glow, and ambient highlights. Everything else inherits from the kiosk's base `:root`.

### Best practice: copy `themes/dark/theme.css`

Use `themes/dark/theme.css` as a starter. It mirrors the kiosk `:root` defaults and gives you every variable in one place. Edit what you want and delete the rest — the kiosk's `:root` fills any variable you omit.

### Decorative layers (optional)

Beyond CSS variables you can add per-theme rules that target real DOM elements. Examples already in tree:

- `themes/japandi/theme.css` adds gradients, SVG patterns, and pseudo-element overlays on the body and `.ambient-grid`.
- `themes/matrix/theme.css` toggles the visibility of the `#matrix-rain` `<canvas>` (the canvas itself is part of the kiosk's base HTML).

When you add decorative selectors, scope every one with `body.theme-<name>` so it never bleeds into other themes:

```css
body.theme-myth .ambient-grid {
    background-image: linear-gradient(135deg, rgba(136,176,75,.08), transparent 60%);
}
```

### Per-state overrides

The kiosk sets state classes on `<body>`: `state-idle`, `state-listening`, `state-processing`, `state-speaking`. Theme one state without affecting others:

```css
body.theme-myth.state-processing .eye-core {
    animation: my-pulse 2s ease-in-out infinite alternate;
}
```

(`@keyframes my-pulse` lives in `theme.css` too — animations are scoped CSS, not global.)

---

## CSS variable reference

These are the variables defined in the kiosk's `:root`. Override any in your theme; omit any you don't care about and the default applies.

### Layout & typography

| Variable | Purpose |
|---|---|
| `--bg` | Page background color |
| `--surface` | Slightly raised surface tone (selects, etc.) |
| `--border` | Subtle 1-px borders |
| `--text` | Primary text color |
| `--text-dim` | Secondary text (labels, timestamps) |

### Accent (drives the iris + status + UI affordances)

| Variable | Purpose |
|---|---|
| `--accent` | Main accent hex (`#ff2d2d` for PAL red) |
| `--accent-rgb` | Same color as comma-separated rgb tuple (used in `rgba(var(--accent-rgb), 0.x)` shadows) |
| `--accent-glow` | Lighter halo color |
| `--accent-glow-rgb` | RGB triple form |
| `--accent-deep` | Deepest shadow side of the accent |
| `--accent-warm` | Secondary warm tint for transitions |
| `--accent-warm-rgb` | RGB triple form |
| `--success` | Success/positive indicator color |

### Bezel (the metallic ring around the orb)

| Variable | Purpose |
|---|---|
| `--bezel-1`–`--bezel-4` | Four metallic stops, dark→light, drive the conic-gradient ring |
| `--bezel-inner` | Deep recessed cavity behind the lens |
| `--bezel-inner-edge` | Slightly lighter top of the cavity |
| `--bezel-shadow` | Outer drop shadow color |
| `--lens-rim` | Dark contour ring around the glass lens |

### Eye core (the lit orb interior)

| Variable | Purpose |
|---|---|
| `--core-deep-stop` | Deepest gradient stop in the lit core |
| `--core-shadow` | Mid shadow |
| `--core-shadow-deep` | Deep shadow |
| `--iris-highlight` | Bright iris highlight (warm tone usually) |
| `--pupil-1`, `--pupil-2` | Pupil gradient stops (mostly unused — the yellow center light renders via JS) |
| `--reflection` | Specular highlight on the orb glass |

### Speaking state (white-hot core during TTS)

| Variable | Purpose |
|---|---|
| `--speak-core-1`, `--speak-core-2` | Inner two gradient stops |
| `--speak-pupil`, `--speak-pupil-2` | Pupil glow when speaking |
| `--speak-reflection` | Brighter reflection |
| `--speak-glow` | Ambient halo |

### Ambient & misc

| Variable | Purpose |
|---|---|
| `--scan-line` | The faint scan-line that pulses during the processing state |
| `--grid-line` | The faint background grid color |
| `--ambient-radial` | Diffuse radial color behind the orb |
| `--status-speak` | Status indicator dot color when speaking |
| `--status-speak-glow` | Status dot halo |
| `--vol-hover-bg` | Hover background on volume / mute buttons |

---

## `effect.js` (optional)

Themes with `"effect": "<file>.js"` in their manifest provide a dynamic background or overlay. The kiosk imports the module *on first activation* and keeps the instance for the lifetime of the page; subsequent activations and deactivations call `start()` and `stop()` on the same controller.

### Contract

```js
// themes/myth/effect.js
export default function setup({ root }) {
    // `root` is the kiosk's <body>. You may mount canvases, divs,
    // listeners, anything — but you MUST clean up in stop().

    let raf = null;

    return {
        start() {
            // Called when the theme activates. Idempotent — guard if
            // already running.
            if (raf !== null) return;
            // … begin your animation / overlay …
            const tick = () => { raf = requestAnimationFrame(tick); /* draw */ };
            raf = requestAnimationFrame(tick);
        },

        stop() {
            // Called when the theme deactivates. Must release every
            // resource start() acquired — RAF, intervals, listeners,
            // DOM nodes, MediaStream tracks, anything.
            if (raf !== null) cancelAnimationFrame(raf);
            raf = null;
            // … remove your DOM additions, drop listeners …
        },
    };
}
```

### Rules of the road

- **Default export must be a function.** Returns an object with `start()` and `stop()`.
- **`setup()` runs once** per page lifetime (first activation). `start()`/`stop()` cycle on each theme swap.
- **`stop()` must fully clean up.** The user can switch themes at any time. Leaks pile up.
- **Don't lean on existing DOM elements you didn't add.** The kiosk's structural HTML (orb, controls, transcript) is stable but treat it as someone else's UI. The `<canvas id="matrix-rain">` element is one published exception — see below.
- **No network calls.** Effects should be self-contained client-side animations.
- **No third-party imports.** Bare-specifier imports won't resolve (no bundler). Inline everything you need.

### Published DOM hooks

The kiosk's base HTML reserves one element for theme effects:

- `<canvas id="matrix-rain">` — viewport-sized, `pointer-events: none`, `z-index: 0` (behind the orb). Default `opacity: 0`. Show it from your `theme.css` (see `themes/matrix/theme.css` for the `opacity: 0.55` rule) and animate it from your `effect.js`. Coordinate the visibility CSS and the animation in your theme so both come and go together.

### Worked example

See `themes/matrix/effect.js` for a complete production-quality animation (digital rain): DPR-aware resize, trail fade, head-of-column highlight, RAF lifecycle, resize listener cleanup.

---

## Installation & lifecycle

### How the registry picks up your theme

1. Drop the folder into `server/themes/`.
2. The server's `ThemeRegistry` polls the directory every `THEMES_POLL_INTERVAL_S` seconds (default 10).
3. When it sees a new directory (or a change in any existing `manifest.json`, `theme.css`, or `effect.js`), it diffs by fingerprint.
4. On change: republishes MQTT discovery so HA selects refresh their options, and emits a `themes_changed` WebSocket message.
5. The kiosk receives `themes_changed`, re-fetches `/api/themes`, and rebuilds its dropdown.
6. The first time someone selects your theme, the kiosk lazy-injects `<link rel="stylesheet" href="/themes/<name>/theme.css">` and (if your manifest declares one) `import("/themes/<name>/effect.js")`.

### What survives a kiosk refresh

The kiosk caches no theme assets — every CSS/JS file is served with `Cache-Control: no-cache`. Edit `theme.css`, refresh the kiosk, your changes appear.

### What survives a theme uninstall

If you remove your theme's directory while it's the active theme on a kiosk, the next `themes_changed` event will trigger a fallback to `dark` on every kiosk.

---

## Validation

The server is forgiving:

- Missing `manifest.json` or `theme.css` → the directory is silently skipped (with a debug log).
- Malformed JSON in `manifest.json` → the directory is skipped (with a warning).
- `effect` set to a non-existent file → `has_effect` is `false`; the theme still loads as colors-only.
- Invalid `kind` → defaults to `dark`.
- Filename traversal in static-file requests (`../etc/passwd` etc.) → 404.

Nothing about an invalid theme can take the server down. Worst case: it doesn't appear in the catalog.

---

## Testing your theme

### Locally without redeploying

If you're running the stack with `docker-compose.server.yml` (build locally), the host directory `./server/themes/<name>/` is bind-mounted live into the container. Edits show up within one polling tick.

### Quick checklist before shipping

- [ ] `manifest.json` parses cleanly (`python -m json.tool < manifest.json`)
- [ ] `theme.css` body selector matches the folder name exactly
- [ ] At minimum `--bg`, `--accent`, `--text` are visually distinct from existing themes
- [ ] If you add `effect.js`: `stop()` releases everything `start()` allocated (test by toggling themes 20× and watching the JS heap)
- [ ] All decorative selectors scoped with `body.theme-<name>` — no global leaks
- [ ] Animations don't run when the theme isn't active (check by switching away and confirming no CPU activity in DevTools)

### Three-tier behavior matrix

For each tier, your theme should look acceptable:

| State | What's on screen |
|---|---|
| `idle` | The orb at rest with subtle ambient breath |
| `listening` | Wake-detected — orb rings pulse with `--accent` |
| `processing` | Inner core spins / pulses — most themes use a warm pulse |
| `speaking` | White-hot core during TTS using `--speak-*` variables |

---

## Reference: themes shipped in-tree

| Theme | Folder | Notes |
|---|---|---|
| dark | `themes/dark/` | Baseline PAL red. Mirrors the kiosk `:root`. |
| sal | `themes/sal/` | Cyan on blue-black. |
| glados | `themes/glados/` | Aperture amber on warm black. |
| matrix | `themes/matrix/` | Phosphor green + `effect.js` digital rain. |
| mother | `themes/mother/` | *Alien* Nostromo industrial amber. |
| joi | `themes/joi/` | *Blade Runner 2049* pink on teal. |
| kitt | `themes/kitt/` | Knight Rider red on chrome. |
| birch | `themes/birch/` | Warm beige light theme. |
| odyssey | `themes/odyssey/` | Pure white minimal. |
| japandi | `themes/japandi/` | Heaviest decorative example — gradients + SVG patterns + pseudo-elements. |
| forest | `themes/forest/` | Moss green on walnut. |
| sunset | `themes/sunset/` | Coral on plum. |

Read any of these as reference implementations.

---

## REST API (theme catalog)

The kiosk consumes this; you can hit it too for tooling or external installers.

### `GET /api/themes`

Returns the catalog. Both the AI server (`http://<ai-server>:8765/api/themes`) and the audio_streamer proxy (`http://<rpi>:8080/api/themes`) expose it identically.

```json
{
    "themes": [
        {
            "name": "matrix",
            "display_name": "Matrix — Phosphor green",
            "description": "Phosphor green on pitch black — old-CRT terminal feel with cascading digital rain.",
            "version": "1.0.0",
            "kind": "dark",
            "has_effect": true
        },
        // …
    ]
}
```

### `GET /themes/<name>/<filename>`

Serves `theme.css`, `effect.js`, or any other file inside a theme directory. Filename is sanitized — only direct children are reachable (no traversal).

Response has `Cache-Control: no-cache, no-store, must-revalidate` so kiosks always see your latest edits.

---

## FAQ

**Q: Can I ship a theme via HACS / a script?**
The mount is read-write — a future installer could drop folders into `server/themes/` and the registry would pick them up. No installer ships today; do it manually for now.

**Q: Can themes change anything other than CSS variables?**
Yes — any CSS selector is fair game in `theme.css`. Scope everything with `body.theme-<name>`. effect.js can mount arbitrary DOM. Just be a good citizen and clean up in `stop()`.

**Q: Can I use ES module imports from `effect.js`?**
Only relative imports inside your theme folder. Bare-specifier imports (e.g., `import "react"`) won't resolve — there's no bundler.

**Q: Will my theme break across kiosk Chromium upgrades?**
The CSS variable contract is stable; we don't rename them. Decorative HTML hooks (`#matrix-rain`) and state classes (`state-*`) are stable too. If anything changes we'll keep aliases.

**Q: Where do I report bugs / share themes?**
Open an issue / PR on the [conversation-hass](https://github.com/moimart/conversation-hass) GitHub repo.
