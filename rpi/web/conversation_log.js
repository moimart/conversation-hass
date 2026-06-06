/* Conversation log overlay — a full-screen scrollable history of user
   requests, PAL's answers, and announcements, fetched from the server's
   persistent log (GET /api/conversation/log) and rendered onto the
   #conversation-log-root element. Driven by show_conversation_log /
   hide_conversation_log messages (and opened locally by the mobile app's
   log button).

   Mirrors calendar.js: mountConversationLog(root) -> { show(payload),
   update(payload), dismiss(reason), isShown() }. dismiss() resolves when
   the stage has rotated back.

   Kiosk: auto-dismisses after duration_s (default 30s) of NO interaction —
   any touch/scroll inside the view resets the timer + countdown bar.
   Mobile (window.HAL_CONFIG present): no auto-dismiss; an explicit ✕
   button closes the view. */

const PAGE_SIZE = 100;
const LOAD_THRESHOLD_PX = 80;   // scroll-top distance that triggers older-page load
const MAX_RENDERED_ROWS = 1500; // stop paging beyond this (hint shown)

export function mountConversationLog(root) {
    if (!root) throw new Error("mountConversationLog: root is required");

    const HAL = window.HAL_CONFIG || null;   // present = mobile companion
    const isMobile = !!HAL;

    let dismissTimer = null;
    let countdownStart = 0;
    let countdownDuration = 0;
    let countdownRaf = null;
    let currentDuration = 30;
    let pendingDismissPromise = null;
    let pendingDismissResolve = null;
    let dismissSafetyTimeout = null;
    let dismissTransitionListener = null;
    let dismissTransitionTarget = null;

    // paging state
    let oldestId = null;
    let hasMore = false;
    let loadingOlder = false;
    let renderedRows = 0;

    root.innerHTML = "";
    const overlay = document.createElement("div");
    overlay.className = "clog-overlay";
    overlay.innerHTML = `
        <div class="clog-header">
          <div class="clog-header-titles">
            <div class="clog-source">CONVERSATION LOG</div>
            <div class="clog-title"></div>
          </div>
          <button class="clog-close" aria-label="Close" style="display:none">&#x2715;</button>
          <div class="clog-countdown-bar"></div>
        </div>
        <div class="clog-body"></div>
    `;
    root.appendChild(overlay);

    const titleEl = overlay.querySelector(".clog-title");
    const bodyEl = overlay.querySelector(".clog-body");
    const countdownEl = overlay.querySelector(".clog-countdown-bar");
    const closeEl = overlay.querySelector(".clog-close");

    if (isMobile) {
        closeEl.style.display = "";
        countdownEl.style.display = "none";
        closeEl.addEventListener("click", () => dismiss("close"));
    }

    function base() {
        return (HAL && HAL.serverBaseUrl) ? HAL.serverBaseUrl.replace(/\/+$/, "") : "";
    }
    function authHeaders() {
        return (HAL && HAL.token) ? { Authorization: `Bearer ${HAL.token}` } : {};
    }

    function clearTimers() {
        if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }
        if (countdownRaf) { cancelAnimationFrame(countdownRaf); countdownRaf = null; }
    }

    function clearDismissState(reason) {
        if (dismissSafetyTimeout) { clearTimeout(dismissSafetyTimeout); dismissSafetyTimeout = null; }
        if (dismissTransitionTarget && dismissTransitionListener) {
            dismissTransitionTarget.removeEventListener("transitionend", dismissTransitionListener);
        }
        dismissTransitionTarget = null;
        dismissTransitionListener = null;
        if (pendingDismissResolve) {
            try { pendingDismissResolve(reason || "cleared"); } catch (_) {}
            pendingDismissResolve = null;
        }
        pendingDismissPromise = null;
    }

    function startCountdown(durationS) {
        countdownStart = performance.now();
        countdownDuration = durationS * 1000;
        countdownEl.style.transform = "scaleX(1)";
        function step(now) {
            const elapsed = now - countdownStart;
            const remaining = Math.max(0, 1 - elapsed / countdownDuration);
            countdownEl.style.transform = `scaleX(${remaining})`;
            if (remaining > 0 && dismissTimer) {
                countdownRaf = requestAnimationFrame(step);
            }
        }
        countdownRaf = requestAnimationFrame(step);
    }

    function scheduleAutoDismiss(durationS) {
        if (isMobile) return;                 // mobile: explicit close only
        if (dismissTimer) clearTimeout(dismissTimer);
        if (countdownRaf) cancelAnimationFrame(countdownRaf);
        startCountdown(durationS);
        dismissTimer = setTimeout(() => dismiss("timeout"), durationS * 1000);
    }

    // Any interaction inside the view keeps it alive (kiosk only).
    for (const evName of ["pointerdown", "wheel", "touchmove"]) {
        bodyEl.addEventListener(evName, () => {
            if (!isMobile && isShown()) scheduleAutoDismiss(currentDuration);
        }, { passive: true });
    }

    /* === data + rendering ================================================ */

    async function fetchPage(beforeId) {
        const qs = new URLSearchParams({ limit: String(PAGE_SIZE) });
        if (beforeId != null) qs.set("before_id", String(beforeId));
        const res = await fetch(`${base()}/api/conversation/log?${qs}`, {
            headers: authHeaders(), cache: "no-store",
        });
        if (!res.ok) throw new Error(`log fetch -> ${res.status}`);
        return res.json();
    }

    function rowDateLabel(iso) {
        const d = new Date(iso);
        return d.toLocaleDateString(undefined, {
            weekday: "short", month: "short", day: "numeric",
        });
    }
    function rowDateKey(iso) {
        const d = new Date(iso);
        return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
    }
    function rowTime(iso) {
        const d = new Date(iso);
        return String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0");
    }

    function buildRow(row) {
        const el = document.createElement("div");
        el.className = `clog-row ${row.kind}`;
        el.dataset.dateKey = row.ts ? rowDateKey(row.ts) : "";
        const time = document.createElement("span");
        time.className = "clog-time";
        time.textContent = row.ts ? rowTime(row.ts) : "";
        if (row.ts) time.title = row.ts;
        el.appendChild(time);
        const text = document.createElement("span");
        text.className = "clog-text";
        if (row.kind === "image" && row.has_image) {
            // Orb images render as a small lazy-loaded thumbnail — the page
            // payload only says has_image; bytes come from the image route.
            const img = document.createElement("img");
            img.className = "clog-img";
            img.loading = "lazy";
            img.alt = row.text || "Image shown on the orb";
            img.src = `${base()}/api/conversation/log/image?id=${row.id}`;
            text.appendChild(img);
        } else {
            text.textContent = row.text;
        }
        if (row.origin) {
            // Origin chip rides INLINE at the end of the message — a leading
            // chip column squeezes the text and makes mixed rows look ragged.
            const chip = document.createElement("span");
            chip.className = "clog-origin";
            chip.textContent = row.origin;
            text.appendChild(chip);
        }
        el.appendChild(text);
        return el;
    }

    function buildDaySep(iso) {
        const el = document.createElement("div");
        el.className = "clog-day-sep";
        el.dataset.dateKey = rowDateKey(iso);
        el.textContent = rowDateLabel(iso);
        return el;
    }

    // Build an ASC block of rows (+ day separators on local-date change).
    // prevKey: dateKey of whatever precedes this block (null = none).
    function buildBlock(rows, prevKey) {
        const frag = document.createDocumentFragment();
        let lastKey = prevKey;
        for (const row of rows) {
            const key = row.ts ? rowDateKey(row.ts) : lastKey;
            if (row.ts && key !== lastKey) {
                frag.appendChild(buildDaySep(row.ts));
                lastKey = key;
            }
            frag.appendChild(buildRow(row));
        }
        return { frag, lastKey };
    }

    function setNotice(cls, text, retryable = false) {
        bodyEl.innerHTML = "";
        const el = document.createElement("div");
        el.className = `clog-empty ${cls || ""}`;
        el.textContent = text;
        if (retryable) {
            el.style.cursor = "pointer";
            el.addEventListener("click", () => initialLoad());
        }
        bodyEl.appendChild(el);
    }

    async function initialLoad(attempt = 0) {
        bodyEl.innerHTML = "";
        renderedRows = 0;
        oldestId = null;
        hasMore = false;
        setNotice("", "Loading…");
        let data;
        try {
            data = await fetchPage(null);
        } catch (e) {
            console.warn("[clog] fetch failed:", e);
            // Cold-start grace: a tap right after app launch can race the
            // network stack — retry once automatically, then tap-to-retry.
            if (attempt < 1 && isShown()) {
                setTimeout(() => { if (isShown()) initialLoad(attempt + 1); }, 1500);
                return;
            }
            setNotice("error", "Conversation log unavailable — tap to retry.", true);
            return;
        }
        if (data.disabled) {
            setNotice("", "Conversation logging is not configured.");
            return;
        }
        if (!data.rows || data.rows.length === 0) {
            setNotice("", "No conversation history yet.");
            return;
        }
        bodyEl.innerHTML = "";
        const { frag } = buildBlock(data.rows, null);
        bodyEl.appendChild(frag);
        renderedRows = data.rows.length;
        oldestId = data.rows[0].id;
        hasMore = !!data.has_more;
        bodyEl.scrollTop = bodyEl.scrollHeight;   // open at the bottom (newest)
    }

    async function loadOlder() {
        if (loadingOlder || !hasMore || oldestId == null) return;
        if (renderedRows >= MAX_RENDERED_ROWS) {
            hasMore = false;
            const hint = document.createElement("div");
            hint.className = "clog-day-sep";
            hint.textContent = "(older history truncated)";
            bodyEl.insertBefore(hint, bodyEl.firstChild);
            return;
        }
        loadingOlder = true;
        // Capture the anchor BEFORE mutating the DOM, restore after prepend:
        // scrollTop += height delta keeps the visible row pinned (no jump).
        const prevHeight = bodyEl.scrollHeight;
        const prevTop = bodyEl.scrollTop;
        try {
            const data = await fetchPage(oldestId);
            const rows = data.rows || [];
            if (rows.length) {
                // Dedupe the boundary day-separator: if the new block's last
                // date equals the existing first row's date, the existing
                // block needs no leading separator — buildBlock handles the
                // INTERNAL separators; we drop the existing leading sep if it
                // duplicates the prepended block's trailing date.
                const { frag, lastKey } = buildBlock(rows, null);
                const firstExisting = bodyEl.firstElementChild;
                if (firstExisting && firstExisting.classList.contains("clog-day-sep")
                        && firstExisting.dataset.dateKey === lastKey) {
                    firstExisting.remove();
                }
                bodyEl.insertBefore(frag, bodyEl.firstChild);
                bodyEl.scrollTop = prevTop + (bodyEl.scrollHeight - prevHeight);
                renderedRows += rows.length;
                oldestId = rows[0].id;
            }
            hasMore = !!data.has_more && rows.length > 0;
        } catch (e) {
            console.warn("[clog] older-page fetch failed:", e);
        } finally {
            loadingOlder = false;
        }
    }

    bodyEl.addEventListener("scroll", () => {
        if (bodyEl.scrollTop < LOAD_THRESHOLD_PX) loadOlder();
    }, { passive: true });

    /* === show / dismiss (calendar.js pattern) ============================ */

    function show(payload) {
        clearTimers();
        clearDismissState("cancelled");
        currentDuration = (payload && payload.duration_s) || 30;
        titleEl.textContent = new Date().toLocaleDateString(undefined, {
            weekday: "long", month: "long", day: "numeric",
        });
        document.body.classList.add("show-conversation-log");
        scheduleAutoDismiss(currentDuration);
        initialLoad();
    }

    function update(payload) {
        currentDuration = (payload && payload.duration_s) || currentDuration;
        scheduleAutoDismiss(currentDuration);
        initialLoad();
    }

    function dismiss(reason = "explicit") {
        clearTimers();
        if (!document.body.classList.contains("show-conversation-log")) {
            clearDismissState("already-hidden");
            return Promise.resolve(reason);
        }
        if (pendingDismissPromise) return pendingDismissPromise;

        pendingDismissPromise = new Promise((resolve) => {
            pendingDismissResolve = resolve;
            const stage = document.querySelector(".clog-stage");
            const onEnd = (ev) => {
                if (ev && ev.propertyName && ev.propertyName !== "transform") return;
                clearDismissState(reason);
            };
            dismissTransitionListener = onEnd;
            dismissTransitionTarget = stage;
            if (stage) stage.addEventListener("transitionend", onEnd);
            dismissSafetyTimeout = setTimeout(() => onEnd(null), 1000);
            document.body.classList.remove("show-conversation-log");
        });
        return pendingDismissPromise;
    }

    function isShown() {
        return document.body.classList.contains("show-conversation-log");
    }

    return { show, update, dismiss, isShown };
}
