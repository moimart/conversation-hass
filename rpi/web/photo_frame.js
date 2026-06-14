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
    let faceAnim = null;     // running WAA animation (or null)
    let faceLayer = null;    // the <img> the face anim is attached to
    let curFaces = null;     // { faces:[{x,y,w,h}], iw, ih } for the active photo
    let resizeBound = false;

    function cancelFacePan() {
        if (faceAnim) { try { faceAnim.cancel(); } catch (_) { /* ignore */ } faceAnim = null; }
        if (faceLayer) { faceLayer.classList.add("ken-burns"); faceLayer = null; }
    }

    // Transform that centers a normalized face box under object-fit:cover at a
    // moderate zoom, clamped so the (already-covering) image never exposes an
    // edge. transform-origin is the viewport centre (CSS .pf-img: 50% 50%).
    function faceTransform(box, geom, zoomMul) {
        const { vw, vh, iw, ih } = geom;
        const s = Math.max(vw / iw, vh / ih);     // object-fit: cover scale
        const dw = iw * s, dh = ih * s;            // displayed (covering) size
        const ox = (vw - dw) / 2, oy = (vh - dh) / 2;  // displayed top-left (≤0)
        const cx = ox + (box.x + box.w / 2) * dw;  // face centre, screen px
        const cy = oy + (box.y + box.h / 2) * dh;
        const faceH = box.h * dh;
        // Moderate framing: aim for the face to fill ~1/3 of the height.
        let Z = faceH > 0 ? (vh / 3) / faceH : 1.3;
        Z = Math.min(1.8, Math.max(1.15, Z)) * (zoomMul || 1);
        let tx = Z * (vw / 2 - cx);                // bring face centre → centre
        let ty = Z * (vh / 2 - cy);
        // Coverage clamp (translate is in post-scale screen px):
        const ax = Z * vw / 2, ay = Z * vh / 2;
        const txMax = -vw / 2 - Z * ox + ax;
        const txMin = vw / 2 - Z * (ox + dw) + ax;
        const tyMax = -vh / 2 - Z * oy + ay;
        const tyMin = vh / 2 - Z * (oy + dh) + ay;
        tx = Math.min(txMax, Math.max(txMin, tx));
        ty = Math.min(tyMax, Math.max(tyMin, ty));
        return `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${Z.toFixed(4)})`;
    }

    function startFacePan() {
        if (!curFaces || !curFaces.faces.length) return;
        const layer = active;                      // the currently-shown photo
        const vw = root.clientWidth, vh = root.clientHeight;
        const iw = layer.naturalWidth || curFaces.iw;
        const ih = layer.naturalHeight || curFaces.ih;
        if (!vw || !vh || !iw || !ih) return;      // not laid out / decoded yet
        cancelFacePan();                           // stop any prior anim
        const geom = { vw, vh, iw, ih };
        const N = curFaces.faces.length;
        let keyframes, opts;
        if (N === 1) {
            // Single face: gentle breathing zoom centred on it.
            keyframes = [
                { transform: faceTransform(curFaces.faces[0], geom, 1.0) },
                { transform: faceTransform(curFaces.faces[0], geom, 1.12) },
            ];
            opts = { duration: 9000, iterations: Infinity,
                     direction: "alternate", easing: "ease-in-out" };
        } else {
            // Pan across faces in order, dwelling on each, looping back to the
            // first. Dwell = flat segment (identical transforms); the moves
            // between faces are eased.
            keyframes = [];
            const hold = 0.55;                     // fraction of each slot held
            for (let i = 0; i <= N; i++) {
                const t = faceTransform(curFaces.faces[i % N], geom, 1.0);
                const base = i / N;
                keyframes.push({ transform: t, offset: Math.min(1, base) });
                if (i < N) keyframes.push({ transform: t, offset: Math.min(1, base + hold / N) });
            }
            opts = { duration: N * 4500, iterations: Infinity, easing: "ease-in-out" };
        }
        layer.classList.remove("ken-burns");       // hand off to the JS anim
        try {
            faceAnim = layer.animate(keyframes, opts);
            faceLayer = layer;
        } catch (_) {
            layer.classList.add("ken-burns");      // WAA unavailable → default
            faceAnim = null; faceLayer = null;
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
        const faces = msg && Array.isArray(msg.faces) ? msg.faces : [];
        if (!shown || faces.length === 0) { curFaces = null; cancelFacePan(); return; }
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
        // New photo → drop any face pan; the new image starts on the default
        // Ken Burns until its own photo_faces arrives.
        curFaces = null; cancelFacePan();
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
    }

    async function update(payload) {
        if (!shown) {
            // update arrived before show — treat it as a show.
            return show(payload);
        }
        curFaces = null; cancelFacePan();
        await paintInto(buffer, payload);
        buffer.classList.remove("ken-burns");
        void buffer.offsetWidth;
        buffer.classList.add("ken-burns");
        buffer.classList.add("active");
        active.classList.remove("active");
        [active, buffer] = [buffer, active];
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
            curFaces = null; cancelFacePan();
            // Wait for the CSS fade-out (matches the 400ms in style.css).
            setTimeout(() => {
                shown = false;
                // Clear images so a fresh show always paints from scratch
                // (avoids a cached old image flashing in).
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
