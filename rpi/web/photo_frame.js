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

    // Stage layout: two image layers + clock-display + nothing else.
    // The two layers crossfade on update — back layer becomes the
    // active one, front fades out and is reused next time.
    root.innerHTML = "";
    const imgA = root.ownerDocument.createElement("img");
    const imgB = root.ownerDocument.createElement("img");
    imgA.alt = "";
    imgB.alt = "";
    imgA.className = "pf-img active";  // currently visible
    imgB.className = "pf-img";          // hidden, used for the next swap
    root.appendChild(imgA);
    root.appendChild(imgB);

    const clock = root.ownerDocument.createElement("div");
    clock.className = "clock-display pf-clock";
    clock.innerHTML = '<div class="clock-time pf-clock-time">--:--</div>'
                    + '<div class="clock-date pf-clock-date">---</div>';
    root.appendChild(clock);

    const timeEl = clock.querySelector(".clock-time");
    const dateEl = clock.querySelector(".clock-date");

    let active = imgA;  // pointer to the currently-displayed layer
    let buffer = imgB;  // pointer to the layer that holds the NEXT image
    let shown = false;
    let clockTimer = null;

    // Refresh the clock+date once per second. Berlin Type is loaded
    // globally by style.css so we don't need to do anything but write
    // text content.
    function refreshClock() {
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, "0");
        const mm = String(now.getMinutes()).padStart(2, "0");
        timeEl.textContent = `${hh}:${mm}`;
        const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
        const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
        dateEl.textContent = `${days[now.getDay()]} ${now.getDate()} ${months[now.getMonth()]}`;
    }
    refreshClock();

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
            if (!clockTimer) clockTimer = setInterval(refreshClock, 1000);
        }
        refreshClock();
    }

    async function update(payload) {
        if (!shown) {
            // update arrived before show — treat it as a show.
            return show(payload);
        }
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
                if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
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

    return { show, update, dismiss, isShown };
}
