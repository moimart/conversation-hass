/**
 * HAL 9000 — Web UI Client
 *
 * Connects via WebSocket to the local RPi audio streamer service
 * to receive live transcription and AI responses.
 */

(() => {
    "use strict";

    // --- Server config (kiosk vs mobile reuse) ---
    // The kiosk serves this UI from the RPi and talks to it same-origin, so
    // window.HAL_CONFIG is undefined and every helper below falls back to
    // location.host / relative paths (unchanged kiosk behaviour). The mobile
    // Capacitor shell injects window.HAL_CONFIG = {serverBaseUrl, wsUrl, token}
    // BEFORE this script runs, pointing the same UI at the AI server directly.
    const HAL = (typeof window !== "undefined" && window.HAL_CONFIG) || {};
    // Base URL for server HTTP (themes, api). "" → same-origin (kiosk).
    function halBase() { return HAL.serverBaseUrl || ""; }
    // Full WebSocket URL. Kiosk: ws(s)://<host>/ws (the RPi). Mobile:
    // HAL_CONFIG.wsUrl (the server's /ws/ui), token appended as a query param.
    function halWsUrl() {
        if (HAL.wsUrl) {
            return HAL.token
                ? HAL.wsUrl + (HAL.wsUrl.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(HAL.token)
                : HAL.wsUrl;
        }
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        return `${protocol}//${location.host}/ws`;
    }

    // --- DOM Elements ---
    const transcript = document.getElementById("transcript");
    const responseContainer = document.getElementById("response-container");
    const responseText = document.getElementById("response-text");
    const connectionEl = document.getElementById("connection");
    const statusText = document.querySelector(".status-text");
    const themeSelect = document.getElementById("theme-select");

    // --- State ---
    let ws = null;
    let reconnectTimer = null;
    let currentPartialEl = null;
    const MAX_TRANSCRIPT_LINES = 50;

    // --- Theme registry (plug-in folders served from the server) ---
    // The kiosk knows nothing about which themes exist at compile time —
    // it fetches /api/themes on startup, lazy-loads each theme's CSS on
    // first activation, and lazy-imports its optional effect.js module.
    const THEME_KEY = "hal-theme";
    const ORIENTATION_KEY = "hal-orientation";
    const ORB_SIDE_KEY = "hal-orb-side";
    let themes = [];                 // [{name, display_name, has_effect, kind, state_videos?, ...}]
    let loadedCss = new Set();       // names whose stylesheet <link> we've already injected
    let loadedEffects = new Map();   // name -> { start, stop } controller from effect.js
    let currentTheme = null;
    // Lazy-imported state-video controller (rpi/web/state_videos.js).
    // Cached so we don't re-import every theme switch.
    let stateVideoModule = null;
    let stateVideoController = null;

    function injectThemeCss(name) {
        if (loadedCss.has(name)) return;
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = `${halBase()}/themes/${encodeURIComponent(name)}/theme.css`;
        link.dataset.theme = name;
        document.head.appendChild(link);
        loadedCss.add(name);
    }

    async function ensureEffect(name) {
        if (loadedEffects.has(name)) return loadedEffects.get(name);
        try {
            const mod = await import(`${halBase()}/themes/${encodeURIComponent(name)}/effect.js`);
            const setup = mod.default;
            if (typeof setup !== "function") return null;
            const ctrl = setup({ root: document.getElementById("orientation-wrapper") || document.body });
            loadedEffects.set(name, ctrl);
            return ctrl;
        } catch (e) {
            console.warn(`theme ${name}: effect.js failed to load:`, e);
            return null;
        }
    }

    function stopAllEffects() {
        for (const ctrl of loadedEffects.values()) {
            try { ctrl.stop && ctrl.stop(); } catch (e) { /* ignore */ }
        }
    }

    function applyOrientation(orientation, orbSide) {
        // The mobile shell pins landscape (no wrapper rotation) so the kiosk's
        // configured portrait mounting doesn't render the phone UI sideways.
        if (HAL.pinLandscape) orientation = "landscape";
        orientation = (orientation || "landscape").toLowerCase();
        orbSide = (orbSide || "left").toLowerCase();
        const wrapper = document.getElementById("orientation-wrapper");
        if (!wrapper) return;

        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const zoom = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;

        document.body.classList.remove(
            "orientation-portrait", "orientation-landscape",
            "orb-left", "orb-right"
        );
        document.body.classList.add(`orientation-${orientation}`);
        if (orientation === "landscape") {
            document.body.classList.add(`orb-${orbSide}`);
        }

        if (orientation === "portrait") {
            const w = vh / zoom;
            const h = vw / zoom;
            wrapper.style.width = w + "px";
            wrapper.style.height = h + "px";
            wrapper.style.left = ((vw / zoom - w) / 2) + "px";
            wrapper.style.top = ((vh / zoom - h) / 2) + "px";
            wrapper.style.transform = "rotate(90deg)";
            document.documentElement.style.setProperty("--wrapper-h", h + "px");
        } else {
            const w = vw / zoom;
            const h = vh / zoom;
            wrapper.style.width = w + "px";
            wrapper.style.height = h + "px";
            wrapper.style.left = "0";
            wrapper.style.top = "0";
            wrapper.style.transform = "none";
            document.documentElement.style.setProperty("--wrapper-h", h + "px");
        }

        localStorage.setItem(ORIENTATION_KEY, orientation);
        localStorage.setItem(ORB_SIDE_KEY, orbSide);

        // Effects listen for window resize to re-size their canvases.
        // Wrapper dimension changes don't trigger that, so fire it manually.
        window.dispatchEvent(new Event("resize"));
    }

    async function applyTheme(name) {
        if (!name || typeof name !== "string") name = "dark";
        // Trust the requested name — even if the catalog isn't loaded
        // yet, lazy-loading the CSS via /themes/<name>/theme.css will
        // either succeed (the theme exists server-side) or 404 silently.
        injectThemeCss(name);
        document.body.classList.forEach(c => {
            if (c.startsWith("theme-")) document.body.classList.remove(c);
        });
        document.body.classList.add(`theme-${name}`);
        if (themeSelect) themeSelect.value = name;
        stopAllEffects();
        const theme = themes.find(t => t.name === name);
        if (theme && theme.has_effect) {
            const ctrl = await ensureEffect(name);
            try { ctrl && ctrl.start && ctrl.start(); } catch (e) { console.warn(e); }
        }
        // State-video layer: tear down whatever the previous theme had,
        // then mount fresh if the new theme declares state_videos.
        if (stateVideoController) {
            try { stateVideoController.destroy(); } catch (e) { /* ignore */ }
            stateVideoController = null;
        }
        const sv = theme && theme.state_videos;
        if (sv && typeof sv === "object" && Object.keys(sv).length > 0) {
            try {
                if (!stateVideoModule) {
                    stateVideoModule = await import("/state_videos.js");
                }
                const eyeContainer = document.querySelector(".eye-container");
                if (eyeContainer && stateVideoModule.mountStateVideos) {
                    stateVideoController = stateVideoModule.mountStateVideos(
                        eyeContainer, name, sv,
                    );
                    // Sync immediately so first paint shows the right clip
                    // (lookup uses the body class set above).
                    const stateClass = Array.from(document.body.classList)
                        .find(c => c.startsWith("state-"));
                    if (stateClass && stateVideoController) {
                        stateVideoController.setState(stateClass.slice("state-".length));
                    }
                }
            } catch (e) {
                console.warn(`theme ${name}: state_videos mount failed:`, e);
            }
        }
        currentTheme = name;
    }

    function rebuildThemeDropdown() {
        if (!themeSelect) return;
        const currentValue = themeSelect.value;
        themeSelect.innerHTML = "";
        for (const t of themes) {
            const opt = document.createElement("option");
            opt.value = t.name;
            opt.textContent = t.display_name || t.name;
            themeSelect.appendChild(opt);
        }
        if (currentValue && themes.some(t => t.name === currentValue)) {
            themeSelect.value = currentValue;
        }
    }

    async function loadThemes() {
        try {
            const r = await fetch(`${halBase()}/api/themes`, { cache: "no-store" });
            const data = await r.json();
            themes = Array.isArray(data.themes) ? data.themes : [];
        } catch (e) {
            console.warn("theme list fetch failed:", e);
            themes = [{ name: "dark", display_name: "PAL — Dark", has_effect: false, kind: "dark" }];
        }
        rebuildThemeDropdown();
    }

    if (themeSelect) {
        themeSelect.addEventListener("change", (e) => {
            const choice = e.target.value;
            localStorage.setItem(THEME_KEY, choice);
            applyTheme(choice);
        });
    }

    // Fetch the catalog, then apply the saved theme (or dark by default).
    loadThemes().then(() => {
        const saved = localStorage.getItem(THEME_KEY) || "dark";
        applyTheme(saved);
    });

    // Restore orientation from localStorage (server pushes authoritative value on connect).
    applyOrientation(
        localStorage.getItem(ORIENTATION_KEY) || "landscape",
        localStorage.getItem(ORB_SIDE_KEY) || "left"
    );

    // Reapply wrapper dimensions on resize (e.g., if Chromium resizes).
    window.addEventListener("resize", () => {
        applyOrientation(
            localStorage.getItem(ORIENTATION_KEY) || "landscape",
            localStorage.getItem(ORB_SIDE_KEY) || "left"
        );
    });

    // --- WebSocket ---
    function connect() {
        const url = halWsUrl();

        ws = new WebSocket(url);

        ws.onopen = () => {
            console.log("Connected to HAL");
            connectionEl.className = "connection-indicator connected";
            connectionEl.querySelector("span").textContent = "CONNECTED";
            clearTimeout(reconnectTimer);
        };

        ws.onclose = () => {
            console.log("Disconnected");
            connectionEl.className = "connection-indicator";
            connectionEl.querySelector("span").textContent = "DISCONNECTED";
            setState("idle");
            scheduleReconnect();
        };

        ws.onerror = (err) => {
            console.error("WebSocket error:", err);
            ws.close();
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                console.error("Failed to parse message:", e);
            }
        };
    }

    function scheduleReconnect() {
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 3000);
    }

    // --- Calendar overlay state (lazy-loaded on first show_calendar) ---
    let calendarController = null;     // returned by mountCalendar()
    let calendarLoading = null;        // dynamic import promise

    async function getCalendar() {
        if (calendarController) return calendarController;
        if (!calendarLoading) {
            calendarLoading = import("./calendar.js").then((mod) => {
                const root = document.getElementById("calendar-root");
                calendarController = mod.mountCalendar(root);
                return calendarController;
            }).catch((e) => {
                console.error("[calendar] module load failed:", e);
                calendarLoading = null;
                throw e;
            });
        }
        return calendarLoading;
    }

    // --- Conversation log overlay (lazy-loaded on first show) ---
    let conversationLogController = null;
    let conversationLogLoading = null;
    async function getConversationLog() {
        if (conversationLogController) return conversationLogController;
        if (!conversationLogLoading) {
            conversationLogLoading = import("./conversation_log.js").then((mod) => {
                const root = document.getElementById("conversation-log-root");
                conversationLogController = mod.mountConversationLog(root);
                return conversationLogController;
            }).catch((e) => {
                console.error("[clog] module load failed:", e);
                conversationLogLoading = null;
                throw e;
            });
        }
        return conversationLogLoading;
    }
    // Mobile companion opens the view locally (log button in the input bar).
    window.HALConversationLog = {
        open: () => getConversationLog().then((c) => c.show({ duration_s: 0 })),
        close: () => { if (conversationLogController) conversationLogController.dismiss("close"); },
    };

    // Wrap any orb-takeover handler so the calendar dismisses first.
    // Returns a Promise that resolves once the takeover handler has been
    // invoked (after the cube has rotated back, if it was up).
    async function withCalendarPreempt(runFn) {
        if (calendarController && calendarController.isShown()) {
            try {
                await calendarController.dismiss("preempt");
            } catch (e) {
                console.warn("[calendar] preempt dismiss failed, continuing:", e);
            }
        }
        return runFn();
    }

    // --- Photo frame overlay state (lazy-loaded on first show_photo_frame) ---
    let pfController = null;
    let pfLoading = null;

    // The image and video controllers each own a SEPARATE sub-layer inside
    // #photo-frame-root. They used to share the root and both did
    // innerHTML="" on mount, so whichever mounted last orphaned the other's
    // elements (a stuck broken-image <img> from the image controller would
    // then sit over a detached, invisible <video>). Separate layers +
    // showing only the active one keeps photo and video modes from
    // clobbering each other. display:contents means the layer adds no box,
    // so .pf-img / .pf-video still position against the stage as before.
    function pfLayer(id) {
        const root = document.getElementById("photo-frame-root");
        let el = document.getElementById(id);
        if (!el) {
            el = document.createElement("div");
            el.id = id;
            el.style.display = "contents";
            root.appendChild(el);
        }
        return el;
    }

    // Show exactly one of the two layers ("img" | "video"), hiding the
    // other so a dismissed/idle controller can't bleed through.
    function pfShowLayer(which) {
        const img = pfLayer("pf-img-layer");
        const vid = pfLayer("pf-video-layer");
        img.style.display = which === "img" ? "contents" : "none";
        vid.style.display = which === "video" ? "contents" : "none";
    }

    // Whether the clock/date overlay is shown during photo mode (server
    // setting, default on). The kiosk keeps body.pf-hide-clock synced to
    // its inverse; the CSS only hides the clock when that class AND
    // .photo-frame-active are both present, so the home-screen clock is
    // never affected. The server pushes the value on connect and on change.
    function applyPhotoFrameClock(show) {
        document.body.classList.toggle("pf-hide-clock", !show);
    }

    async function getPhotoFrame() {
        if (pfController) return pfController;
        if (!pfLoading) {
            pfLoading = import("./photo_frame.js").then((mod) => {
                const root = pfLayer("pf-img-layer");
                pfController = mod.mountPhotoFrame(root, {
                    // When the kiosk dismisses itself (touch / state / etc.),
                    // tell the server so it tears down the HA subscription.
                    onDismiss: (reason) => {
                        if (ws && ws.readyState === WebSocket.OPEN) {
                            try {
                                ws.send(JSON.stringify({
                                    type: "photo_frame_dismissed",
                                    reason: reason,
                                }));
                            } catch (_) { /* ignore */ }
                        }
                    },
                });
                return pfController;
            }).catch((e) => {
                console.error("[photo-frame] module load failed:", e);
                pfLoading = null;
                throw e;
            });
        }
        return pfLoading;
    }

    // --- Looping-video photo frame (parallel controller, same stage) ---
    let pfVideoController = null;
    let pfVideoLoading = null;

    async function getPhotoFrameVideo() {
        if (pfVideoController) return pfVideoController;
        if (!pfVideoLoading) {
            pfVideoLoading = import("./photo_frame.js").then((mod) => {
                const root = pfLayer("pf-video-layer");
                pfVideoController = mod.mountPhotoFrameVideo(root, {
                    // Same dismissal protocol as the image frame.
                    onDismiss: (reason) => {
                        if (ws && ws.readyState === WebSocket.OPEN) {
                            try {
                                ws.send(JSON.stringify({
                                    type: "photo_frame_dismissed",
                                    reason: reason,
                                }));
                            } catch (_) { /* ignore */ }
                        }
                    },
                    // Video couldn't load / autoplay — ask the server to
                    // fall back to cycling photos.
                    onError: (reason) => {
                        if (ws && ws.readyState === WebSocket.OPEN) {
                            try {
                                ws.send(JSON.stringify({
                                    type: "photo_frame_video_error",
                                    reason: reason,
                                }));
                            } catch (_) { /* ignore */ }
                        }
                    },
                });
                return pfVideoController;
            }).catch((e) => {
                console.error("[photo-frame] video module load failed:", e);
                pfVideoLoading = null;
                throw e;
            });
        }
        return pfVideoLoading;
    }

    // Single helper called from every kiosk-initiated dismissal trigger
    // (state change, volume, mute, PTT activation, takeover overlays,
    // pointer/touch). No-op when not shown. Covers BOTH the image and the
    // looping-video frame.
    function maybeDismissPhotoFrame(reason) {
        if (pfController && pfController.isShown()) {
            pfController.dismiss(reason).catch(() => {});
        }
        if (pfVideoController && pfVideoController.isShown()) {
            pfVideoController.dismiss(reason).catch(() => {});
        }
    }

    // Protection window for HAL's own confirmation TTS when the LLM
    // just called show_photo_frame this turn. Set when a show message
    // arrives, cleared when the turn ends (state→idle) or a new user
    // turn begins (state→listening). While the flag is set, speaking
    // and processing transitions don't dismiss — because they're the
    // continuation of the voice command that opened the frame in the
    // first place. Anything OTHER than that turn (a /api/speak
    // announcement, a different voice command later, an HA-fired
    // command) lands with the flag already false and dismisses
    // normally.
    let pfProtectedTurn = false;

    async function withPhotoFramePreempt(runFn) {
        if (pfController && pfController.isShown()) {
            try {
                await pfController.dismiss("preempt");
            } catch (e) {
                console.warn("[photo-frame] preempt dismiss failed, continuing:", e);
            }
        }
        if (pfVideoController && pfVideoController.isShown()) {
            try {
                await pfVideoController.dismiss("preempt");
            } catch (e) {
                console.warn("[photo-frame] video preempt dismiss failed, continuing:", e);
            }
        }
        return runFn();
    }

    // --- Message Handling ---
    function handleMessage(msg) {
        switch (msg.type) {
            case "transcription":
                handleTranscription(msg);
                break;
            case "wake":
                handleWake();
                break;
            case "response":
                handleResponse(msg);
                break;
            case "state":
                // Satellite-only: the server ends the turn (state→idle) as soon
                // as it has handed off the response, but this device plays HAL's
                // TTS locally and finishes ~seconds later. Suppress that early
                // idle so the orb keeps its speaking animation until OUR audio
                // ends (satellite-audio.ts drives the final idle via
                // window.HALSetState). The kiosk has no HALSatelliteAudio, so it
                // is unaffected.
                if (msg.state === "idle" &&
                    window.HALSatelliteAudio &&
                    typeof window.HALSatelliteAudio.isPlaying === "function" &&
                    window.HALSatelliteAudio.isPlaying()) {
                    break;
                }
                setState(msg.state);
                // Photo frame dismiss rules:
                //   listening → always dismiss (user is talking; clear
                //               protection for whatever turn comes next).
                //   idle      → turn just ended; clear protection so the
                //               NEXT non-idle transition dismisses
                //               regardless of source.
                //   speaking
                //   /processing → dismiss UNLESS pfProtectedTurn is set
                //                  (i.e. this is HAL's own confirmation
                //                  TTS for the same voice command that
                //                  opened the frame). Anything else —
                //                  /api/speak announcements, a different
                //                  voice command later, an HA-fired
                //                  command — has pfProtectedTurn=false
                //                  and dismisses normally.
                if (msg.state === "listening") {
                    maybeDismissPhotoFrame("state-listening");
                    pfProtectedTurn = false;
                } else if (msg.state === "idle") {
                    pfProtectedTurn = false;
                } else if (msg.state === "speaking" || msg.state === "processing") {
                    if (!pfProtectedTurn) {
                        maybeDismissPhotoFrame("state-" + msg.state);
                    }
                }
                break;
            case "mute_sync": {
                // Mute state sync. Only treat it as a dismiss trigger when
                // the value ACTUALLY changes — the server re-asserts mute on
                // every (e.g. 30-min) reconnect, and a redundant re-sync must
                // not tear down an active photo/video frame.
                const newMuted = !!msg.muted;
                const muteChanged = newMuted !== muted;
                muted = newMuted;
                muteBtn.classList.toggle("muted", muted);
                muteIconOn.style.display = muted ? "none" : "";
                muteIconOff.style.display = muted ? "" : "none";
                if (muteChanged) maybeDismissPhotoFrame("mute");
                break;
            }
            case "volume_sync": {
                // Same as mute_sync: only a real change dismisses the frame,
                // not a reconnect re-sync of the current level.
                const newVolume = Math.max(0, Math.min(1, msg.level));
                const volChanged = newVolume !== volume;
                volume = newVolume;
                volFill.style.width = (volume * 100) + "%";
                if (volChanged) maybeDismissPhotoFrame("volume");
                break;
            }
            case "pong":
                break;
            case "set_theme":
                if (typeof msg.name === "string" && msg.name) {
                    localStorage.setItem(THEME_KEY, msg.name);
                    applyTheme(msg.name);
                }
                break;
            case "set_orientation":
                // Ignored on mobile (pinned landscape); honored on the kiosk.
                if (!HAL.pinLandscape) applyOrientation(msg.orientation, msg.orb_side);
                break;
            case "set_photo_frame_clock":
                // Whether the clock/date overlay shows DURING photo mode
                // (user setting, default on). Keep body.pf-hide-clock in
                // sync; the CSS only acts on it under .photo-frame-active,
                // so this is safe to apply at any time and takes effect
                // live if a frame is already on screen.
                applyPhotoFrameClock(msg.show !== false);
                break;
            case "themes_changed":
                loadThemes().then(() => {
                    if (currentTheme && !themes.some(t => t.name === currentTheme)) {
                        applyTheme("dark");
                    }
                });
                break;
            case "show_camera":
                withPhotoFramePreempt(() => withCalendarPreempt(() => showCamera(msg)));
                break;
            case "stream_start":
                withPhotoFramePreempt(() => withCalendarPreempt(() => startStream(msg)));
                break;
            case "stream_stop":
                stopStream();
                break;
            case "webrtc_signal":
                // Signaling messages must NOT block on calendar dismiss —
                // they only matter once a stream is active, and the
                // stream_start that opens the session already preempts.
                handleSignal(msg);
                break;
            case "play_video":
                withPhotoFramePreempt(() => withCalendarPreempt(() => playVideo(msg)));
                break;
            case "video_stop":
                stopVideo();
                break;
            case "show_calendar":
                withPhotoFramePreempt(() => {
                    getCalendar().then((cal) => {
                        if (cal.isShown()) cal.update(msg);
                        else cal.show(msg);
                    }).catch((e) => console.error("[calendar] show failed:", e));
                });
                break;
            case "hide_calendar":
                if (calendarController) {
                    calendarController.dismiss("explicit").catch(() => {});
                }
                break;
            case "show_conversation_log":
                withPhotoFramePreempt(() => withCalendarPreempt(() => {
                    getConversationLog().then((clog) => {
                        if (clog.isShown()) clog.update(msg);
                        else clog.show(msg);
                    }).catch((e) => console.error("[clog] show failed:", e));
                }));
                break;
            case "hide_conversation_log":
                if (conversationLogController) {
                    conversationLogController.dismiss("explicit").catch(() => {});
                }
                break;
            case "ptt_active":
                // Hint-only: flip a body class so the PTT chip + orb
                // glow show up. The server-side PTT trigger is owned
                // by an external app/hardware/HA — there's no kiosk
                // input for it.
                document.body.classList.toggle("ptt-active", !!msg.active);
                if (msg.active) {
                    maybeDismissPhotoFrame("ptt");
                }
                break;
            case "show_photo_frame":
                // Protect through HAL's confirmation TTS for THIS turn —
                // see the "state" case above. Flip to the image layer so a
                // previously-shown video can't bleed through.
                pfProtectedTurn = true;
                pfShowLayer("img");
                // The clock-during-photo-mode preference rides with the show
                // payload (a browser that reconnected late can't rely on the
                // connect-time push), so apply it as the frame opens.
                applyPhotoFrameClock(msg.show_clock !== false);
                if (pfVideoController) pfVideoController.dismiss("explicit").catch(() => {});
                getPhotoFrame().then((pf) => pf.show(msg))
                    .catch((e) => console.error("[photo-frame] show failed:", e));
                break;
            case "show_photo_frame_video":
                pfProtectedTurn = true;
                pfShowLayer("video");
                applyPhotoFrameClock(msg.show_clock !== false);
                if (pfController) pfController.dismiss("explicit").catch(() => {});
                getPhotoFrameVideo().then((pf) => pf.show(msg))
                    .catch((e) => console.error("[photo-frame] video show failed:", e));
                break;
            case "photo_frame_update":
                if (msg.show_clock !== undefined) applyPhotoFrameClock(msg.show_clock !== false);
                if (pfController) pfController.update(msg);
                break;
            case "hide_photo_frame":
                pfProtectedTurn = false;
                if (pfController) pfController.dismiss("explicit").catch(() => {});
                if (pfVideoController) pfVideoController.dismiss("explicit").catch(() => {});
                break;
            case "show_pairing_code":
                // Mobile device pairing: the server minted a code; show it
                // fullscreen so the user can type it into the companion app.
                if (window.HALPairingOverlay) window.HALPairingOverlay.show(msg.code, msg.expires_in);
                break;
            case "hide_pairing_code":
                if (window.HALPairingOverlay) window.HALPairingOverlay.hide();
                break;
            case "tts_play":
                // Satellite mode: play HAL's response audio on THIS device (the
                // server cached it; the mobile audio module fetches + plays it).
                // No-op on the kiosk — it has no HALSatelliteAudio global and is
                // never sent tts_play anyway (it's a device-targeted message).
                if (window.HALSatelliteAudio) window.HALSatelliteAudio.play(msg.url, msg.mime);
                break;
            default:
                console.log("Unknown message:", msg);
        }
    }

    function handleTranscription(msg) {
        const { text, is_partial, speaker } = msg;

        if (!text || !text.trim()) return;

        // Remove placeholder
        const placeholder = transcript.querySelector(".transcript-placeholder");
        if (placeholder) placeholder.remove();

        if (is_partial) {
            // Update or create partial line
            if (!currentPartialEl) {
                currentPartialEl = document.createElement("div");
                currentPartialEl.className = "transcript-line partial latest";
                transcript.appendChild(currentPartialEl);
            }
            currentPartialEl.textContent = text;
            setState("listening");
        } else {
            // Finalize: replace partial with final
            if (currentPartialEl) {
                currentPartialEl.remove();
                currentPartialEl = null;
            }

            // Remove 'latest' from previous lines
            transcript.querySelectorAll(".latest").forEach(el => el.classList.remove("latest"));

            const line = document.createElement("div");
            line.className = "transcript-line latest";
            if (speaker === "ai") {
                line.style.color = "var(--accent)";
                line.style.opacity = "0.5";
            }
            line.textContent = text;
            transcript.appendChild(line);

            // Prune old lines
            while (transcript.children.length > MAX_TRANSCRIPT_LINES) {
                transcript.removeChild(transcript.firstChild);
            }
        }

        // Auto-scroll
        transcript.scrollTop = transcript.scrollHeight;
    }

    function handleWake() {
        // Flash the eye and show wake indicator
        const eye = document.querySelector(".eye-container");
        eye.classList.add("wake-flash");
        setState("listening");

        // Remove flash after animation completes
        setTimeout(() => eye.classList.remove("wake-flash"), 800);
    }

    function handleResponse(msg) {
        const { text } = msg;
        if (!text) return;

        responseText.textContent = text;
        // Stamp the moment HAL replied — kiosk-local time, UI-only, never
        // part of the spoken text or stored response.
        const ts = document.getElementById("response-timestamp");
        if (ts) {
            const now = new Date();
            ts.textContent = now.toLocaleTimeString(undefined, {
                hour: "2-digit",
                minute: "2-digit",
            });
        }
        responseContainer.classList.add("visible");
        setState("speaking");
    }

    // --- Camera-in-orb display ---
    let cameraTimer = null;
    function showCamera(msg) {
        const container = document.querySelector(".eye-container");
        const inner = document.querySelector(".eye-bezel-inner");
        if (!container || !inner || !msg.image) return;
        // Snapshot replaces any active live stream.
        stopStream();
        const mime = msg.mime || "image/jpeg";
        const dur = Math.max(5, Math.min(900, Number(msg.duration_s) || 150));
        if (cameraTimer) clearTimeout(cameraTimer);
        inner.style.backgroundImage = `url("data:${mime};base64,${msg.image}")`;
        container.classList.add("camera-active");
        container.classList.remove("stream-active");
        cameraTimer = setTimeout(clearCamera, dur * 1000);
    }
    function clearCamera() {
        const container = document.querySelector(".eye-container");
        const inner = document.querySelector(".eye-bezel-inner");
        if (inner) inner.style.backgroundImage = "";
        if (container) container.classList.remove("camera-active");
        cameraTimer = null;
    }

    // --- Live WebRTC stream-in-orb ---
    // The kiosk owns the RTCPeerConnection. The server proxies signaling
    // to Home Assistant's camera/webrtc/offer subscription.
    let streamPC = null;
    let streamSessionId = null;
    let streamMode = "trickle";
    // Satellite-only WebRTC→MJPEG fallback (auto-detect): if the peer
    // connection can't reach the camera's LAN ICE candidates within the
    // watchdog window (remote phone without a VPN/subnet route), tear it down
    // and load the server-proxied MJPEG instead — everything then flows
    // through the single server URL. The kiosk never takes this path
    // (no HAL_CONFIG, and its LAN connection always succeeds).
    let streamFallbackTimer = null;
    let streamFallbackImg = null;
    function streamFallbackToMjpeg() {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (!container || !streamSessionId || !window.HAL_CONFIG) return;
        if (streamFallbackTimer) { clearTimeout(streamFallbackTimer); streamFallbackTimer = null; }
        if (streamPC) { try { streamPC.close(); } catch (e) { /* ignore */ } streamPC = null; }
        if (video) { try { video.srcObject = null; } catch (e) { /* ignore */ } }
        if (!streamFallbackImg) {
            const img = document.createElement("img");
            // Same class as the <video> → inherits the in-orb sizing and the
            // .stream-active visibility toggle; appended after it, so it
            // renders on top of the (black, srcObject-less) video element.
            img.className = "eye-stream";
            img.id = "eye-stream-fallback";
            (video && video.parentElement ? video.parentElement : container).appendChild(img);
            streamFallbackImg = img;
        }
        const tok = HAL.token ? "token=" + encodeURIComponent(HAL.token) + "&" : "";
        streamFallbackImg.src = halBase() + "/api/satellite/stream.mjpeg?" + tok
            + "sid=" + encodeURIComponent(streamSessionId);
        container.classList.add("camera-active", "stream-active");
        console.log("Stream: WebRTC unreachable — falling back to server-proxied MJPEG");
    }
    function startStream(msg) {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (!container || !video) return;
        // A new stream replaces any existing one (snapshot or stream).
        clearCamera();
        stopStream();
        streamSessionId = msg.session_id || "";
        // "trickle" (HA): send offer immediately, stream candidates as
        //   they arrive. "non-trickle" (go2rtc): wait for ICE gathering
        //   to finish, then send a single bundled offer (the candidates
        //   are already inside the SDP).
        streamMode = msg.mode === "non-trickle" ? "non-trickle" : "trickle";
        const pc = new RTCPeerConnection();
        streamPC = pc;
        pc.addTransceiver("video", { direction: "recvonly" });
        pc.ontrack = (ev) => {
            if (ev.streams && ev.streams[0]) video.srcObject = ev.streams[0];
        };
        pc.onicecandidate = (ev) => {
            if (streamMode !== "trickle") return;
            if (!ev.candidate || !ws || ws.readyState !== WebSocket.OPEN) return;
            ws.send(JSON.stringify({
                type: "webrtc_signal",
                session_id: streamSessionId,
                kind: "candidate",
                candidate: ev.candidate.candidate,
                sdpMid: ev.candidate.sdpMid,
                sdpMLineIndex: ev.candidate.sdpMLineIndex,
            }));
        };
        pc.onconnectionstatechange = () => {
            if (pc.connectionState === "connected") {
                if (streamFallbackTimer) { clearTimeout(streamFallbackTimer); streamFallbackTimer = null; }
                return;
            }
            if (pc.connectionState === "failed" || pc.connectionState === "closed") {
                // Satellite: a failed ICE means the camera's LAN candidates are
                // unreachable from this network — switch to the proxied MJPEG.
                if (window.HAL_CONFIG && pc.connectionState === "failed" && streamSessionId) {
                    streamFallbackToMjpeg();
                } else {
                    stopStream();
                }
            }
        };
        if (window.HAL_CONFIG) {
            if (streamFallbackTimer) clearTimeout(streamFallbackTimer);
            streamFallbackTimer = setTimeout(() => {
                streamFallbackTimer = null;
                if (streamPC && streamPC.connectionState !== "connected") {
                    streamFallbackToMjpeg();
                }
            }, 8000);
        }
        container.classList.add("camera-active", "stream-active");
        const sendOffer = () => {
            if (!ws || ws.readyState !== WebSocket.OPEN || !pc.localDescription) return;
            ws.send(JSON.stringify({
                type: "webrtc_signal",
                session_id: streamSessionId,
                kind: "offer",
                sdp: pc.localDescription.sdp,
            }));
        };
        const waitForIce = () => new Promise((resolve) => {
            if (pc.iceGatheringState === "complete") return resolve();
            pc.addEventListener("icegatheringstatechange", function onChange() {
                if (pc.iceGatheringState === "complete") {
                    pc.removeEventListener("icegatheringstatechange", onChange);
                    resolve();
                }
            });
        });
        pc.createOffer()
            .then((offer) => pc.setLocalDescription(offer))
            .then(() => {
                if (streamMode === "non-trickle") return waitForIce().then(sendOffer);
                sendOffer();
            })
            .catch((e) => {
                console.warn("WebRTC offer failed:", e);
                stopStream();
            });
    }
    function handleSignal(msg) {
        if (!streamPC || !streamSessionId || msg.session_id !== streamSessionId) return;
        if (msg.kind === "answer" && msg.sdp) {
            streamPC.setRemoteDescription({ type: "answer", sdp: msg.sdp }).catch((e) => {
                console.warn("setRemoteDescription failed:", e);
            });
        } else if (msg.kind === "candidate" && msg.candidate) {
            streamPC.addIceCandidate({
                candidate: msg.candidate,
                sdpMid: msg.sdpMid || null,
                sdpMLineIndex: msg.sdpMLineIndex == null ? null : msg.sdpMLineIndex,
            }).catch((e) => {
                console.debug("addIceCandidate ignored:", e);
            });
        }
    }
    function stopStream() {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (streamFallbackTimer) {
            clearTimeout(streamFallbackTimer);
            streamFallbackTimer = null;
        }
        if (streamFallbackImg) {
            try { streamFallbackImg.src = ""; streamFallbackImg.remove(); } catch (e) { /* ignore */ }
            streamFallbackImg = null;
        }
        if (streamPC) {
            try { streamPC.close(); } catch (e) { /* ignore */ }
            streamPC = null;
        }
        streamSessionId = null;
        if (video) {
            try { video.srcObject = null; } catch (e) { /* ignore */ }
        }
        if (container) {
            container.classList.remove("stream-active");
            // Drop camera-active too if no snapshot is on screen
            if (!cameraTimer) container.classList.remove("camera-active");
        }
    }

    // --- HTTP video playback (MP4 / WebM / HLS) ---
    // The kiosk owns the lifecycle entirely; the server just sends the
    // play_video message and the optional video_stop dismissal. HAL TTS
    // auto-ducks the audio while the assistant is speaking.
    let videoTimer = null;
    let videoHls = null;
    let videoUserMuted = false;
    function isHls(url) {
        return /\.m3u8(?:\?|$)/i.test(url);
    }
    // Probe the kiosk Chromium's codec list. Exposed in transcript
    // alongside any video error so the user can diagnose without
    // opening DevTools (which the kiosk often hides).
    function videoCapabilities() {
        const v = document.createElement("video");
        const probes = [
            ["mp4", "video/mp4"],
            ["mp4 h264", 'video/mp4; codecs="avc1.42E01E,mp4a.40.2"'],
            ["webm vp9", 'video/webm; codecs="vp9,opus"'],
            ["webm vp8", 'video/webm; codecs="vp8,vorbis"'],
            ["webm av1", 'video/webm; codecs="av01.0.05M.08"'],
        ];
        return probes.map(([n, t]) => `${n}=${v.canPlayType(t) || "no"}`).join(", ");
    }
    function pushTranscriptLine(text, accent) {
        const placeholder = transcript.querySelector(".transcript-placeholder");
        if (placeholder) placeholder.remove();
        const line = document.createElement("div");
        line.className = "transcript-line latest";
        if (accent) {
            line.style.color = "var(--accent)";
            line.style.fontWeight = "500";
        }
        line.textContent = text;
        transcript.appendChild(line);
        while (transcript.children.length > MAX_TRANSCRIPT_LINES) {
            transcript.removeChild(transcript.firstChild);
        }
        transcript.scrollTop = transcript.scrollHeight;
    }
    function showVideoError(reason) {
        // Surface playback failures as an accent-colored line in the
        // transcript scroll, with the codec capability list so the
        // user can tell at a glance whether the build supports the
        // format they're trying to play.
        pushTranscriptLine(`Video error: ${reason}`, true);
        pushTranscriptLine(`Can play: ${videoCapabilities()}`, true);
        console.warn("Video error:", reason, "| canPlay:", videoCapabilities());
    }
    const MEDIA_ERROR_NAMES = ["UNKNOWN", "ABORTED", "NETWORK", "DECODE", "SRC_NOT_SUPPORTED"];
    function describeMediaError(err) {
        if (!err) return "unknown";
        const code = MEDIA_ERROR_NAMES[err.code] || `code ${err.code}`;
        return err.message ? `${code} — ${err.message}` : code;
    }
    function playVideo(msg) {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (!container || !video || !msg.url) return;
        // Echo so the user can see exactly what URL we're trying to play.
        pushTranscriptLine(`Playing: ${msg.url}`, false);
        // Replace any other modality (snapshot, webrtc stream). Do the
        // video teardown inline WITHOUT calling video.load() — load()
        // aborts the play() we're about to call (the classic Chrome
        // "play() request was interrupted" AbortError). Setting a new
        // src below triggers a fresh load implicitly.
        clearCamera();
        stopStream();
        if (videoTimer) { clearTimeout(videoTimer); videoTimer = null; }
        if (videoHls) {
            try { videoHls.destroy(); } catch (e) { /* ignore */ }
            videoHls = null;
        }
        videoUserMuted = !!msg.muted;
        video.muted = videoUserMuted;
        video.loop = !!msg.loop;
        video.onerror = () => showVideoError(describeMediaError(video.error));
        const finish = () => {
            if (!video.loop) stopVideo();
        };
        const playFail = (e) => {
            // AbortError just means another play/pause/load came along —
            // not a real failure. Anything else is worth surfacing.
            if (e && e.name === "AbortError") return;
            showVideoError(`autoplay blocked or play() failed — ${e && (e.message || e.name) || e}`);
        };
        if (isHls(msg.url) && window.Hls && Hls.isSupported() && !video.canPlayType("application/vnd.apple.mpegurl")) {
            videoHls = new Hls();
            videoHls.loadSource(msg.url);
            videoHls.attachMedia(video);
            videoHls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(playFail));
            videoHls.on(Hls.Events.ERROR, (_, data) => {
                if (data && data.fatal) {
                    showVideoError(`HLS ${data.type || ""} ${data.details || ""}`.trim());
                }
            });
        } else {
            video.src = msg.url;
            video.play().catch(playFail);
        }
        video.addEventListener("ended", finish, { once: true });
        container.classList.add("camera-active", "stream-active");
        if (msg.duration_s) {
            videoTimer = setTimeout(stopVideo, Number(msg.duration_s) * 1000);
        }
    }
    function stopVideo() {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (videoTimer) { clearTimeout(videoTimer); videoTimer = null; }
        if (videoHls) {
            try { videoHls.destroy(); } catch (e) { /* ignore */ }
            videoHls = null;
        }
        if (video) {
            try {
                video.onerror = null;  // suppress synthetic error from src removal
                video.pause();
                video.removeAttribute("src");
                video.load();
            } catch (e) { /* ignore */ }
        }
        videoUserMuted = false;
        if (container) {
            container.classList.remove("stream-active");
            if (!cameraTimer) container.classList.remove("camera-active");
        }
    }

    // --- State Management ---
    function setState(state) {
        // Remove only state-* classes — preserve theme-* and any others
        Array.from(document.body.classList).forEach(c => {
            if (c.startsWith("state-")) document.body.classList.remove(c);
        });

        const labels = {
            idle: "IDLE",
            listening: "LISTENING",
            processing: "PROCESSING",
            speaking: "SPEAKING",
        };

        if (state && labels[state]) {
            document.body.classList.add(`state-${state}`);
            statusText.textContent = labels[state];
        }

        // Crossfade the per-theme state-video to match.
        if (stateVideoController && state) {
            try { stateVideoController.setState(state); } catch (_) { /* ignore */ }
        }

        // Hide previous response when HAL starts thinking about a new question
        if (state === "processing" && responseContainer.classList.contains("visible")) {
            clearTimeout(setState._fadeTimer);
            responseContainer.classList.remove("visible");
        }

        // Cancel any pending fade (we want the response to linger)
        if (state === "speaking") {
            clearTimeout(setState._fadeTimer);
        }

        // Auto-duck a playing HTTP video when HAL is speaking; restore the
        // user's original muted preference when HAL goes back to idle.
        const video = document.getElementById("eye-stream");
        if (video && (video.src || video.currentSrc)) {
            if (state === "speaking") {
                video.muted = true;
            } else if (state === "idle") {
                video.muted = videoUserMuted;
            }
        }
    }

    // Let the satellite TTS player drive the orb directly (speaking while ITS
    // audio plays, idle when it ends). Harmless on the kiosk (never called).
    window.HALSetState = setState;

    // --- Mute Control ---
    let muted = false;
    const muteBtn = document.getElementById("mute-btn");
    const muteIconOn = document.getElementById("mute-icon-on");
    const muteIconOff = document.getElementById("mute-icon-off");

    function setMuted(m) {
        muted = m;
        muteBtn.classList.toggle("muted", muted);
        muteIconOn.style.display = muted ? "none" : "";
        muteIconOff.style.display = muted ? "" : "none";
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "mute", muted: muted }));
        }
        maybeDismissPhotoFrame("kiosk-mute");
    }

    muteBtn.addEventListener("click", () => setMuted(!muted));

    // --- Volume Control ---
    let volume = 0.7;
    const volFill = document.getElementById("vol-fill");
    const volTrack = document.getElementById("vol-track");
    const volDown = document.getElementById("vol-down");
    const volUp = document.getElementById("vol-up");

    function setVolume(v) {
        volume = Math.max(0, Math.min(1, v));
        volFill.style.width = (volume * 100) + "%";
        // Send volume to RPi
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "volume", level: volume }));
        }
        maybeDismissPhotoFrame("kiosk-volume");
    }

    volDown.addEventListener("click", () => setVolume(volume - 0.1));
    volUp.addEventListener("click", () => setVolume(volume + 0.1));

    // Touch/click drag on the bar
    function handleVolDrag(e) {
        const rect = volTrack.getBoundingClientRect();
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        setVolume(pct);
    }

    volTrack.addEventListener("mousedown", (e) => {
        handleVolDrag(e);
        const onMove = (ev) => handleVolDrag(ev);
        const onUp = () => { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    volTrack.addEventListener("touchstart", (e) => {
        handleVolDrag(e);
        const onMove = (ev) => { ev.preventDefault(); handleVolDrag(ev); };
        const onEnd = () => { document.removeEventListener("touchmove", onMove); document.removeEventListener("touchend", onEnd); };
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onEnd);
    });

    // --- Keepalive ---
    setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
        }
    }, 30000);

    // Snapshots are now captured by audio_streamer via the Chrome
    // DevTools Protocol against the running kiosk Chromium. The
    // /api/snapshot proxy still accepts uploads from anywhere for
    // back-compat, but the kiosk page no longer publishes its own.

    // --- Clock + date ---
    const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
    const MONTHS = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ];
    function updateClock() {
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, "0");
        const mm = String(now.getMinutes()).padStart(2, "0");
        const time = document.getElementById("clock-time");
        const date = document.getElementById("clock-date");
        if (time) time.textContent = `${hh}:${mm}`;
        if (date) {
            date.textContent = `${DAYS[now.getDay()]} ${now.getDate()} ${MONTHS[now.getMonth()]}`;
        }
    }
    updateClock();
    // Align next tick to the top of the next minute, then run every 60s.
    const msToNextMinute = 60000 - (Date.now() % 60000);
    setTimeout(() => {
        updateClock();
        setInterval(updateClock, 60000);
    }, msToNextMinute);

    // --- Init ---
    setVolume(0.7);
    setState("idle");
    connect();
})();
