// Timer countdown overlay: the last 10 seconds of a voice timer rendered as a
// big ticking number INSIDE the orb. Server pushes `timer_countdown` with
// ends_at_epoch_ms (only to the device that created the timer); the client
// computes the remaining seconds locally every frame so server/client clock
// drift never skews the tick. Self-dismisses at 0 — `timer_countdown_dismiss`
// / `timer_countdown_cancel` are safety nets (early cancel, fire race).

export function mountTimerOverlay(root) {
    root.innerHTML = `
        <div class="timer-countdown-number">10</div>
        <div class="timer-countdown-name"></div>`;
    const numEl = root.querySelector(".timer-countdown-number");
    const nameEl = root.querySelector(".timer-countdown-name");

    let endsAt = 0;
    let currentId = null;
    let raf = null;
    let lastShown = null;

    function tick() {
        const left = Math.ceil((endsAt - Date.now()) / 1000);
        if (left <= 0) {
            dismiss(currentId);
            return;
        }
        const shown = String(Math.max(0, left));
        if (shown !== lastShown) {
            lastShown = shown;
            numEl.textContent = shown;
            // retrigger the per-second pulse
            numEl.classList.remove("tick");
            void numEl.offsetWidth;
            numEl.classList.add("tick");
        }
        raf = requestAnimationFrame(tick);
    }

    function show(msg) {
        currentId = msg.timer_id || null;
        endsAt = Number(msg.ends_at_epoch_ms) || (Date.now() + (msg.remaining_s || 10) * 1000);
        nameEl.textContent = msg.name || "";
        lastShown = null;
        root.hidden = false;
        document.body.classList.add("show-timer-countdown");
        if (raf) cancelAnimationFrame(raf);
        tick();
    }

    function dismiss(timerId) {
        // Ignore stale dismissals for a timer we're no longer showing.
        if (timerId && currentId && timerId !== currentId) return;
        if (raf) { cancelAnimationFrame(raf); raf = null; }
        currentId = null;
        document.body.classList.remove("show-timer-countdown");
        root.hidden = true;
    }

    function isShown() {
        return !root.hidden;
    }

    return { show, dismiss, isShown };
}
