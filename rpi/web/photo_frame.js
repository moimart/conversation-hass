/* Photo frame overlay — ambient full-screen image with a clock+date
   overlay in white (drop-shadow for legibility on any photo) and a
   slow Ken-Burns zoom.

   Lifecycle: app.js lazy-imports this on the first show_photo_frame
   message, mounts the controller into #photo-frame-root, and feeds
   it show/update/dismiss calls from the WebSocket. The controller
   maintains two stacked <img> layers for crossfading on updates,
   and a pointerdown listener on the stage so a tap dismisses it.

   The kiosk-side dismissal protocol: when the user (or a state
   change) dismisses the frame, the controller calls back into
   app.js's onDismiss callback so app.js can forward a
   photo_frame_dismissed message upstream — the server uses that
   to tear down its HA state_changed subscription. */

export function mountPhotoFrame(root, { onDismiss } = {}) {
    if (!root) throw new Error("mountPhotoFrame: root is required");

    // Stage layout: just two image layers for crossfade. The kiosk's
    // existing top-left .clock-display (already ticking, already
    // Berlin Type, already positioned) is re-used — body.photo-frame-active
    // flips its colour to white + drop-shadow via CSS, so the same
    // element transitions in and out without needing a duplicate
    // here. This also keeps the clock z-ordered above the stage (the
    // body-level clock is at z-index:12 vs the stage at 8).
    root.innerHTML = "";
    const imgA = root.ownerDocument.createElement("img");
    const imgB = root.ownerDocument.createElement("img");
    imgA.alt = "";
    imgB.alt = "";
    imgA.className = "pf-img active";  // currently visible
    imgB.className = "pf-img";          // hidden, used for the next swap
    root.appendChild(imgA);
    root.appendChild(imgB);

    let active = imgA;  // pointer to the currently-displayed layer
    let buffer = imgB;  // pointer to the layer that holds the NEXT image
    let shown = false;

    // --- Face-aware Ken Burns ---------------------------------------------
    // When the server sends photo_faces for the shown photo, we replace the
    // default CSS .ken-burns on the active layer with a JS (Web Animations API)
    // pan that sweeps across the detected faces in order, looping until the
    // photo changes. Empty faces / no faces ⇒ revert to the default effect.
    let curFaces = null;     // { faces:[{x,y,w,h}], iw, ih } for the active photo
    let resizeBound = false;

    // Cancel only the JS (face) animations on a layer — never the CSS Ken Burns
    // (CSSAnimation has an animationName; WAA animations don't). Used to clean a
    // layer BEFORE it's reused; we never cancel the outgoing layer while it's
    // still visible (that would snap it back mid-crossfade — the "jump").
    function clearFaceAnims(layer) {
        if (!layer) return;
        layer.getAnimations().forEach((a) => {
            if (!a.animationName) { try { a.cancel(); } catch (_) { /* ignore */ } }
        });
        layer.style.transform = "";
    }

    // Transform that centers a normalized face box under object-fit:cover at an
    // ABSOLUTE zoom Z, clamped so the (already-covering) image never exposes an
    // edge. transform-origin is the viewport centre (CSS .pf-img: 50% 50%).
    function faceTransform(box, geom, Z) {
        const { vw, vh, iw, ih } = geom;
        const s = Math.max(vw / iw, vh / ih);     // object-fit: cover scale
        const dw = iw * s, dh = ih * s;            // displayed (covering) size
        const ox = (vw - dw) / 2, oy = (vh - dh) / 2;  // displayed top-left (≤0)
        const cx = ox + (box.x + box.w / 2) * dw;  // face centre, screen px
        const cy = oy + (box.y + box.h / 2) * dh;
        let tx = Z * (vw / 2 - cx);                // bring face centre → centre
        let ty = Z * (vh / 2 - cy);
        // Coverage clamp: the <img> is object-fit:cover and clipped to its own
        // vw×vh box, so scaling about the centre by Z lets the box shift at most
        // (Z-1)*half each way before an edge enters the viewport and exposes the
        // black stage. Clamp the translate to that — a face near an edge simply
        // stops at the border (never goes out of bounds), it just isn't centred.
        const halfX = (Z - 1) * vw / 2;
        const halfY = (Z - 1) * vh / 2;
        tx = Math.max(-halfX, Math.min(halfX, tx));
        ty = Math.max(-halfY, Math.min(halfY, ty));
        return `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${Z.toFixed(4)})`;
    }

    // Framing zoom for a multi-face pan: aim for the face to fill ~half the
    // viewport height — enough zoom that the travel between faces actually reads
    // (a shallow zoom makes the pan feel too subtle). Clamped to a sane range.
    function faceZoom(box, geom) {
        const dh = geom.ih * Math.max(geom.vw / geom.iw, geom.vh / geom.ih);
        const faceH = box.h * dh;
        const Z = faceH > 0 ? (geom.vh * 0.48) / faceH : 1.6;
        return Math.min(2.1, Math.max(1.4, Z));
    }

    function startFacePan() {
        if (!curFaces) return;
        const layer = active;                      // the currently-shown photo
        // Use the LAYOUT box (offsetWidth/Height), not getBoundingClientRect:
        // the rect is affected by the element's own (animating) transform AND by
        // any orientation-wrapper rotation, which would feed back an inflated
        // viewport and throw the pan out of bounds. offsetWidth is the un-
        // transformed layout size — the same space object-fit:cover maps into.
        // (root.clientWidth can't be used: the controller root is a
        // display:contents wrapper with zero client size.)
        const vw = layer.offsetWidth || window.innerWidth;
        const vh = layer.offsetHeight || window.innerHeight;
        const iw = layer.naturalWidth || curFaces.iw;
        const ih = layer.naturalHeight || curFaces.ih;
        if (!vw || !vh || !iw || !ih) return;      // not laid out / decoded yet
        clearFaceAnims(layer);                     // clean THIS layer (only)
        const geom = { vw, vh, iw, ih };
        const N = curFaces.faces.length;
        let keyframes, opts;
        if (N === 0) {
            // No faces → a subtle, calm centred zoom in/out (gentle breathing).
            keyframes = [
                { transform: "translate(0px, 0px) scale(1.0)" },
                { transform: "translate(0px, 0px) scale(1.06)" },
            ];
            opts = { duration: 14000, iterations: Infinity,
                     direction: "alternate", easing: "ease-in-out" };
        } else if (N === 1) {
            // Single face → ONE directional Ken Burns (no in/out wobble): push IN
            // on a small/distant face, pull OUT from a large/close one, then hold.
            const f = curFaces.faces[0];
            const s = Math.max(vw / iw, vh / ih);
            const faceBig = (f.h * ih * s) > vh * 0.42;     // face fills >42% height
            const full = faceTransform(f, geom, 1.0);       // whole photo
            const close = faceTransform(f, geom, 1.7);      // zoomed, face centred
            keyframes = faceBig ? [{ transform: close }, { transform: full }]   // pull out
                                : [{ transform: full }, { transform: close }];  // push in
            // Slow + continuous over most of a photo's life so it stays gently
            // in motion (rather than finishing early and sitting static).
            opts = { duration: 36000, iterations: 1, fill: "forwards", easing: "ease-in-out" };
        } else {
            // Multiple faces → pan across them in order: dwell on each, ease to
            // the next, loop back to the first. Deliberate but with visible
            // travel (a middle ground — not frantic, not sleepy).
            const MOVE_MS = 2000, DWELL_MS = 1700;
            const slot = MOVE_MS + DWELL_MS, D = N * slot;
            keyframes = [];
            for (let i = 0; i < N; i++) {
                const t = faceTransform(curFaces.faces[i], geom, faceZoom(curFaces.faces[i], geom));
                const arrive = i * slot;
                keyframes.push({ transform: t, offset: arrive / D });          // arrive
                keyframes.push({ transform: t, offset: (arrive + DWELL_MS) / D }); // dwell
            }
            keyframes.push({ transform: faceTransform(curFaces.faces[0], geom, faceZoom(curFaces.faces[0], geom)), offset: 1 });
            opts = { duration: D, iterations: Infinity, easing: "ease-in-out" };
        }
        layer.classList.remove("ken-burns");       // hand off to the JS anim
        try {
            layer.animate(keyframes, opts);
        } catch (_) {
            layer.classList.add("ken-burns");      // WAA unavailable → default
        }
    }

    function bindResize() {
        if (resizeBound) return;
        resizeBound = true;
        let t = null;
        window.addEventListener("resize", () => {
            if (!shown || !curFaces) return;
            clearTimeout(t);
            t = setTimeout(() => { if (shown && curFaces) startFacePan(); }, 250);
        });
    }

    // Called by app.js on a photo_faces message.
    function setFaces(msg) {
        // Any photo_faces message means the feature is ON (the server stays
        // silent when it's off → default CSS Ken Burns). An empty list = a photo
        // with no detected faces → subtle centred zoom (handled in startFacePan).
        const faces = msg && Array.isArray(msg.faces) ? msg.faces : [];
        if (!shown) { curFaces = null; clearFaceAnims(active); return; }
        curFaces = { faces, iw: msg.image_w || 0, ih: msg.image_h || 0 };
        bindResize();
        startFacePan();
    }

    // Build a data: URL from {image, mime} payload. The server sends
    // base64-encoded bytes so we don't need a fetch round-trip.
    function dataUrl(payload) {
        const mime = payload.mime || "image/jpeg";
        return `data:${mime};base64,${payload.image}`;
    }

    // Pre-load into the buffer layer, then opacity-swap once the
    // browser confirms decode is done. This guarantees no white-flash
    // between the old and new image.
    function paintInto(layer, payload) {
        return new Promise((resolve) => {
            const url = dataUrl(payload);
            layer.onload = () => resolve();
            layer.onerror = () => resolve();   // best-effort — still swap
            layer.src = url;
        });
    }

    async function show(payload) {
        // New photo: clean only the INCOMING layer (buffer). The OUTGOING layer
        // keeps its pan running and fades out smoothly — never cancelled while
        // visible, so there's no snap/jump as the photo changes.
        curFaces = null;
        clearFaceAnims(buffer);
        await paintInto(buffer, payload);
        // Re-trigger Ken-Burns on the incoming layer (animation restarts
        // when we re-add the class).
        buffer.classList.remove("ken-burns");
        void buffer.offsetWidth;   // force reflow
        buffer.classList.add("ken-burns");
        // Swap: buffer becomes active, active becomes hidden buffer.
        buffer.classList.add("active");
        active.classList.remove("active");
        [active, buffer] = [buffer, active];
        if (!shown) {
            shown = true;
            document.body.classList.add("photo-frame-active");
        }
        // Faces for this photo can arrive (synchronously) DURING the async paint
        // above — landing on the pre-swap layer. Re-apply to the now-active one.
        if (curFaces) startFacePan();
    }

    async function update(payload) {
        if (!shown) {
            // update arrived before show — treat it as a show.
            return show(payload);
        }
        curFaces = null;
        clearFaceAnims(buffer);     // incoming only; outgoing fades out un-snapped
        await paintInto(buffer, payload);
        buffer.classList.remove("ken-burns");
        void buffer.offsetWidth;
        buffer.classList.add("ken-burns");
        buffer.classList.add("active");
        active.classList.remove("active");
        [active, buffer] = [buffer, active];
        // Faces may have arrived during the async paint (applied to the pre-swap
        // layer) — re-apply to the now-active layer. This is the common case:
        // detection is fast, so photo_faces lands mid-decode.
        if (curFaces) startFacePan();
    }

    let pendingDismissPromise = null;
    function dismiss(reason = "explicit") {
        if (!shown) return Promise.resolve(reason);
        if (pendingDismissPromise) return pendingDismissPromise;

        pendingDismissPromise = new Promise((resolve) => {
            // Tell app.js so it can forward photo_frame_dismissed upstream
            // (server tears down the HA subscription). Only fire for
            // kiosk-initiated dismissals — server-side hide_photo_frame
            // already knows to clean up.
            if (reason !== "explicit" && typeof onDismiss === "function") {
                try { onDismiss(reason); } catch (_) { /* ignore */ }
            }
            document.body.classList.remove("photo-frame-active");
            curFaces = null;   // let the current pan fade out un-snapped
            // Wait for the CSS fade-out (matches the 400ms in style.css).
            setTimeout(() => {
                shown = false;
                // Clear images so a fresh show always paints from scratch
                // (avoids a cached old image flashing in).
                clearFaceAnims(imgA);
                clearFaceAnims(imgB);
                imgA.removeAttribute("src");
                imgB.removeAttribute("src");
                imgA.classList.remove("ken-burns", "active");
                imgB.classList.remove("ken-burns", "active");
                imgA.classList.add("active");   // reset to canonical layout
                active = imgA;
                buffer = imgB;
                pendingDismissPromise = null;
                resolve(reason);
            }, 450);
        });
        return pendingDismissPromise;
    }

    // Pointer/touch on the stage = dismiss. Listening at the stage root
    // catches taps on the image too.
    root.addEventListener("pointerdown", () => {
        if (shown) dismiss("touch");
    });

    function isShown() {
        return shown;
    }

    return { show, update, dismiss, isShown, setFaces };
}


/* Looping-video photo frame. Plays a single fullscreen <video> on repeat,
   muted (so Chromium autoplays without a user gesture). Reuses the same
   stage (#photo-frame-root), the body.photo-frame-active fade, the white
   clock overlay, and the pointerdown-to-dismiss behaviour as the image
   controller. A separate controller (not the two-<img> crossfade one)
   keeps the play/pause lifecycle clean. */
export function mountPhotoFrameVideo(root, { onDismiss, onError } = {}) {
    if (!root) throw new Error("mountPhotoFrameVideo: root is required");

    root.innerHTML = "";
    const video = root.ownerDocument.createElement("video");
    video.className = "pf-video";
    // Set the muted PROPERTY (not just the attribute) — Chromium only
    // grants gesture-free autoplay when the muted property is true.
    video.muted = true;
    video.loop = true;
    video.autoplay = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.preload = "auto";
    root.appendChild(video);

    let shown = false;
    let pendingDismissPromise = null;

    video.addEventListener("error", () => {
        if (typeof onError === "function") {
            try { onError("load_error"); } catch (_) { /* ignore */ }
        }
    });

    async function show(payload) {
        const src = payload && payload.src;
        if (!src) return;
        // Only reassign src when it actually changes, so a re-show of the
        // same loop doesn't restart playback with a black flash.
        if (video.getAttribute("src") !== src) {
            video.src = src;
        }
        try {
            await video.play();
        } catch (e) {
            // Autoplay blocked (or src failed) — fall back to photos.
            if (typeof onError === "function") {
                try { onError("autoplay_blocked"); } catch (_) { /* ignore */ }
            }
            return;
        }
        if (!shown) {
            shown = true;
            document.body.classList.add("photo-frame-active");
        }
    }

    function dismiss(reason = "explicit") {
        if (!shown) return Promise.resolve(reason);
        if (pendingDismissPromise) return pendingDismissPromise;

        pendingDismissPromise = new Promise((resolve) => {
            if (reason !== "explicit" && typeof onDismiss === "function") {
                try { onDismiss(reason); } catch (_) { /* ignore */ }
            }
            document.body.classList.remove("photo-frame-active");
            // Match the 400ms CSS fade-out before tearing the video down.
            setTimeout(() => {
                shown = false;
                try { video.pause(); } catch (_) { /* ignore */ }
                video.removeAttribute("src");
                try { video.load(); } catch (_) { /* ignore */ }
                pendingDismissPromise = null;
                resolve(reason);
            }, 450);
        });
        return pendingDismissPromise;
    }

    root.addEventListener("pointerdown", () => {
        if (shown) dismiss("touch");
    });

    function isShown() {
        return shown;
    }

    return { show, dismiss, isShown };
}
