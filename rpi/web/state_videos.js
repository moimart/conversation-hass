/* Per-theme state-video controller.

   When the active theme declares a `state_videos` map in its manifest
   (see server/app/themes.py for shape), app.js lazy-imports this
   module and calls mountStateVideos() to populate the
   .eye-state-video-layer placeholder with one <video> per UNIQUE
   source file. The same file referenced from two states (e.g.
   processing and listening sharing listening_320.mp4) results in one
   element with two state-bindings — no duplicate decoders.

   State swaps crossfade via opacity (CSS, 200 ms; see style.css).
   When the kiosk enters camera/stream takeover the CSS hides the
   whole layer; when it enters a fullscreen overlay (calendar /
   photo-frame) the JS-side pause() is invoked automatically by the
   MutationObserver on document.body's class so the active decoder
   doesn't burn cycles behind something the user can't see. */

const FULLSCREEN_OVERLAY_CLASSES = ["show-calendar", "photo-frame-active"];

export function mountStateVideos(container, themeName, videoMap) {
    if (!container) throw new Error("mountStateVideos: container required");
    if (!themeName) throw new Error("mountStateVideos: themeName required");
    if (!videoMap || typeof videoMap !== "object") {
        throw new Error("mountStateVideos: videoMap required");
    }

    const doc = container.ownerDocument || document;

    // Reuse the pre-baked placeholder div if present (added to index.html
    // to reserve the z-stack on first paint). Otherwise create one.
    let layer = container.querySelector(".eye-state-video-layer");
    if (!layer) {
        layer = doc.createElement("div");
        layer.className = "eye-state-video-layer";
        container.appendChild(layer);
    }
    layer.hidden = false;
    layer.innerHTML = "";   // clean slate if remounted

    // Build one <video> per unique source. The stateToEl map binds each
    // state name to the element that should play for it.
    const fileToEl = new Map();        // filename -> <video>
    const stateToEl = new Map();       // state name -> <video>

    for (const [state, filename] of Object.entries(videoMap)) {
        if (typeof filename !== "string" || !filename.trim()) continue;
        let el = fileToEl.get(filename);
        if (!el) {
            el = doc.createElement("video");
            el.src = `/themes/${encodeURIComponent(themeName)}/${encodeURIComponent(filename)}`;
            el.loop = true;
            el.muted = true;
            el.playsInline = true;
            el.autoplay = false;
            el.preload = "auto";
            // Browsers occasionally autoplay anyway when these flags are
            // set in markup order; mute explicitly so the autoplay
            // policy never trips us up.
            el.setAttribute("muted", "");
            el.setAttribute("playsinline", "");
            layer.appendChild(el);
            fileToEl.set(filename, el);
        }
        stateToEl.set(state, el);
    }

    let activeEl = null;
    let currentState = null;
    let pausedByOverlay = false;

    function applyActive(el) {
        if (activeEl === el) return;
        // Fade out the old one
        if (activeEl) {
            activeEl.classList.remove("active");
            // pause on the trailing edge of the crossfade so the decoder
            // for the old clip frees up
            const old = activeEl;
            setTimeout(() => {
                // If state flipped back to this element in the meantime,
                // don't pause it.
                if (old !== activeEl && !old.classList.contains("active")) {
                    try { old.pause(); } catch (_) { /* ignore */ }
                }
            }, 220);
        }
        activeEl = el;
        if (el) {
            el.classList.add("active");
            if (!pausedByOverlay) {
                try {
                    const p = el.play();
                    if (p && typeof p.catch === "function") p.catch(() => {});
                } catch (_) { /* ignore — first interaction may be needed */ }
            }
        }
    }

    function setState(name) {
        currentState = name;
        // Map: processing -> listening as a sensible fallback if the
        // theme doesn't declare a processing clip. Other unmapped
        // states leave the previous video in place (intentional —
        // avoids a static-orb flash during brief transitions).
        let el = stateToEl.get(name);
        if (!el && name === "processing") el = stateToEl.get("listening");
        if (!el) return;
        applyActive(el);
    }

    function pause() {
        pausedByOverlay = true;
        if (activeEl) {
            try { activeEl.pause(); } catch (_) { /* ignore */ }
        }
    }

    function resume() {
        pausedByOverlay = false;
        if (activeEl) {
            try {
                const p = activeEl.play();
                if (p && typeof p.catch === "function") p.catch(() => {});
            } catch (_) { /* ignore */ }
        }
    }

    // Body-class observer: pauses the active video while a fullscreen
    // overlay (calendar, photo frame) is up. The .camera-active /
    // .stream-active classes live on .eye-container, not body, and
    // are handled by CSS (display: none) — no JS needed for those.
    let observer = null;
    function isOverlayActive() {
        return FULLSCREEN_OVERLAY_CLASSES.some((c) => doc.body.classList.contains(c));
    }
    function syncOverlayState() {
        const shouldPause = isOverlayActive();
        if (shouldPause && !pausedByOverlay) pause();
        else if (!shouldPause && pausedByOverlay) resume();
    }
    if (typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(syncOverlayState);
        observer.observe(doc.body, { attributes: true, attributeFilter: ["class"] });
        // Initial sync in case we mount while an overlay is up.
        syncOverlayState();
    }

    function destroy() {
        if (observer) {
            try { observer.disconnect(); } catch (_) { /* ignore */ }
            observer = null;
        }
        for (const el of fileToEl.values()) {
            try { el.pause(); } catch (_) { /* ignore */ }
            try { el.removeAttribute("src"); el.load(); } catch (_) { /* ignore */ }
        }
        fileToEl.clear();
        stateToEl.clear();
        activeEl = null;
        if (layer) {
            layer.innerHTML = "";
            layer.hidden = true;
        }
    }

    return { setState, pause, resume, destroy };
}
