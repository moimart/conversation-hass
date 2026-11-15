/* Voice notes overlay — a full-screen scrollable list of transcribed voice
   notes recorded on satellites, fetched from GET /api/notes and rendered onto
   #notes-root. Driven by show_notes / hide_notes / note_added messages (and
   opened locally by the mobile app's notes button via window.HALNotes).

   A sibling of conversation_log.js: same 3D stage, same card/header/day-sep
   look (it reuses the .clog-* classes for the shared chrome), same keyset
   paging (rows ASC within a page, newest at the bottom, scroll up for older).

   mountNotes(root) -> { show(payload), update(payload), add(row),
   dismiss(reason), isShown() }.

   Kiosk: auto-dismisses after duration_s (default 30s) of no interaction.
   Mobile (window.HAL_CONFIG present): no auto-dismiss, an explicit ✕ closes,
   plus a search field and a per-note delete button. */

const PAGE_SIZE = 100;
const LOAD_THRESHOLD_PX = 80;
const MAX_RENDERED_ROWS = 1500;
const SEARCH_DEBOUNCE_MS = 350;

export function mountNotes(root) {
    if (!root) throw new Error("mountNotes: root is required");

    const HAL = window.HAL_CONFIG || null;   // present = mobile companion
    const isMobile = !!HAL;
    const BODY_CLASS = "show-notes";

    let dismissTimer = null;
    let countdownRaf = null;
    let countdownStart = 0;
    let countdownDuration = 0;
    let currentDuration = 30;
    let pendingDismissPromise = null;
    let pendingDismissResolve = null;
    let dismissSafetyTimeout = null;
    let dismissTransitionListener = null;
    let dismissTransitionTarget = null;

    // paging + search state
    let oldestId = null;
    let hasMore = false;
    let loadingOlder = false;
    let renderedRows = 0;
    let query = "";
    let loadSeq = 0;                 // drops responses from superseded loads
    const renderedIds = new Set();   // dedupe live note_added vs. fetched pages

    root.innerHTML = "";
    const overlay = document.createElement("div");
    overlay.className = "clog-overlay notes-overlay";
    overlay.innerHTML = `
        <div class="clog-header">
          <div class="clog-header-titles">
            <div class="clog-source">VOICE NOTES</div>
            <div class="clog-title"></div>
          </div>
          <input class="notes-search" type="search" placeholder="Search notes"
                 autocomplete="off" enterkeyhint="search" style="display:none">
          <button class="clog-close" aria-label="Close" style="display:none">&#x2715;</button>
          <div class="clog-countdown-bar"></div>
        </div>
        <div class="clog-body"><div class="clog-scroll"></div></div>
    `;
    root.appendChild(overlay);

    const titleEl = overlay.querySelector(".clog-title");
    const bodyEl = overlay.querySelector(".clog-scroll");
    const countdownEl = overlay.querySelector(".clog-countdown-bar");
    const closeEl = overlay.querySelector(".clog-close");
    const searchEl = overlay.querySelector(".notes-search");

    if (isMobile) {
        closeEl.style.display = "";
        searchEl.style.display = "";
        countdownEl.style.display = "none";
        closeEl.addEventListener("click", () => dismiss("close"));
        let searchTimer = null;
        searchEl.addEventListener("input", () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                const q = searchEl.value.trim();
                if (q === query) return;
                query = q;
                initialLoad();
            }, SEARCH_DEBOUNCE_MS);
        });
        searchEl.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); searchEl.blur(); }
        });
    }

    function base() {
        return (HAL && HAL.serverBaseUrl) ? HAL.serverBaseUrl.replace(/\/+$/, "") : "";
    }
    function authHeaders() {
        return (HAL && HAL.token) ? { Authorization: `Bearer ${HAL.token}` } : {};
    }

    /* === timers (conversation_log.js pattern) ============================ */

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
            const remaining = Math.max(0, 1 - (now - countdownStart) / countdownDuration);
            countdownEl.style.transform = `scaleX(${remaining})`;
            if (remaining > 0 && dismissTimer) countdownRaf = requestAnimationFrame(step);
        }
        countdownRaf = requestAnimationFrame(step);
    }

    function scheduleAutoDismiss(durationS) {
        if (isMobile) return;
        if (dismissTimer) clearTimeout(dismissTimer);
        if (countdownRaf) cancelAnimationFrame(countdownRaf);
        startCountdown(durationS);
        dismissTimer = setTimeout(() => dismiss("timeout"), durationS * 1000);
    }

    for (const evName of ["pointerdown", "wheel", "touchmove"]) {
        bodyEl.addEventListener(evName, () => {
            if (!isMobile && isShown()) scheduleAutoDismiss(currentDuration);
        }, { passive: true });
    }

    /* === data + rendering ================================================ */

    async function fetchPage(beforeId) {
        const qs = new URLSearchParams({ limit: String(PAGE_SIZE) });
        if (beforeId != null) qs.set("before_id", String(beforeId));
        if (query) qs.set("q", query);
        const res = await fetch(`${base()}/api/notes?${qs}`, {
            headers: authHeaders(), cache: "no-store",
        });
        if (!res.ok) throw new Error(`notes fetch -> ${res.status}`);
        return res.json();
    }

    function dateLabel(iso) {
        return new Date(iso).toLocaleDateString(undefined, {
            weekday: "short", month: "short", day: "numeric",
        });
    }
    function dateKey(iso) {
        const d = new Date(iso);
        return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
    }
    function timeLabel(iso) {
        const d = new Date(iso);
        return String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0");
    }
    function durationLabel(s) {
        const n = Math.max(0, Math.round(Number(s) || 0));
        return Math.floor(n / 60) + ":" + String(n % 60).padStart(2, "0");
    }

    function buildRow(row) {
        const el = document.createElement("div");
        el.className = "note-row";
        el.dataset.noteId = String(row.id);
        el.dataset.dateKey = row.ts ? dateKey(row.ts) : "";

        const meta = document.createElement("div");
        meta.className = "note-meta";
        const time = document.createElement("span");
        time.className = "clog-time";
        time.textContent = row.ts ? timeLabel(row.ts) : "";
        if (row.ts) time.title = row.ts;
        meta.appendChild(time);
        if (row.duration_s != null) {
            const dur = document.createElement("span");
            dur.className = "note-dur";
            dur.textContent = durationLabel(row.duration_s);
            meta.appendChild(dur);
        }
        if (row.origin) {
            const chip = document.createElement("span");
            chip.className = "clog-origin";
            chip.textContent = row.origin;
            meta.appendChild(chip);
        }
        if (isMobile) {
            const del = document.createElement("button");
            del.className = "note-del";
            del.setAttribute("aria-label", "Delete note");
            del.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>';
            del.addEventListener("click", (e) => {
                e.stopPropagation();
                void deleteNote(row.id, el);
            });
            meta.appendChild(del);
        }
        el.appendChild(meta);

        const text = document.createElement("div");
        text.className = "note-text";
        text.textContent = row.text || "";
        el.appendChild(text);
        return el;
    }

    function buildDaySep(iso) {
        const el = document.createElement("div");
        el.className = "clog-day-sep";
        el.dataset.dateKey = dateKey(iso);
        el.textContent = dateLabel(iso);
        return el;
    }

    function buildBlock(rows, prevKey) {
        const frag = document.createDocumentFragment();
        let lastKey = prevKey;
        for (const row of rows) {
            if (renderedIds.has(row.id)) continue;
            renderedIds.add(row.id);
            const key = row.ts ? dateKey(row.ts) : lastKey;
            if (row.ts && key !== lastKey) {
                frag.appendChild(buildDaySep(row.ts));
                lastKey = key;
            }
            frag.appendChild(buildRow(row));
        }
        return { frag, lastKey };
    }

    function clearBody() {
        bodyEl.innerHTML = "";
        renderedIds.clear();
    }

    function setNotice(cls, text, retryable = false) {
        clearBody();
        const el = document.createElement("div");
        el.className = `clog-empty ${cls || ""}`;
        el.textContent = text;
        if (retryable) {
            el.style.cursor = "pointer";
            el.addEventListener("click", () => initialLoad());
        }
        bodyEl.appendChild(el);
    }

    function emptyText() {
        return query ? `No notes match “${query}”.` : "No voice notes yet.";
    }

    async function initialLoad(attempt = 0) {
        const seq = ++loadSeq;
        renderedRows = 0;
        oldestId = null;
        hasMore = false;
        setNotice("", "Loading…");
        let data;
        try {
            data = await fetchPage(null);
        } catch (e) {
            if (seq !== loadSeq) return;
            console.warn("[notes] fetch failed:", e);
            if (attempt < 1 && isShown()) {
                setTimeout(() => { if (isShown() && seq === loadSeq) initialLoad(attempt + 1); }, 1500);
                return;
            }
            setNotice("error", "Notes unavailable — tap to retry.", true);
            return;
        }
        if (seq !== loadSeq) return;
        if (data.disabled) {
            setNotice("", "Voice notes need the database, which is not configured.");
            return;
        }
        if (!data.rows || data.rows.length === 0) {
            setNotice("", emptyText());
            return;
        }
        clearBody();
        const { frag } = buildBlock(data.rows, null);
        bodyEl.appendChild(frag);
        renderedRows = data.rows.length;
        oldestId = data.rows[0].id;
        hasMore = !!data.has_more;
        stickBottom = true;
        bodyEl.scrollTop = bodyEl.scrollHeight;
        stickToBottomFor(1200);
    }

    async function loadOlder() {
        if (loadingOlder || !hasMore || oldestId == null) return;
        if (renderedRows >= MAX_RENDERED_ROWS) {
            hasMore = false;
            const hint = document.createElement("div");
            hint.className = "clog-day-sep";
            hint.textContent = "(older notes truncated)";
            bodyEl.insertBefore(hint, bodyEl.firstChild);
            return;
        }
        loadingOlder = true;
        const seq = loadSeq;
        const prevHeight = bodyEl.scrollHeight;
        const prevTop = bodyEl.scrollTop;
        try {
            const data = await fetchPage(oldestId);
            if (seq !== loadSeq) return;          // a search / reload superseded us
            const rows = data.rows || [];
            if (rows.length) {
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
            console.warn("[notes] older-page fetch failed:", e);
        } finally {
            loadingOlder = false;
        }
    }

    // Live append (note_added / the mobile recorder's upload response). Only
    // when the view is open and not filtered by a search the note may not match.
    function add(row) {
        if (!row || row.id == null || !isShown() || renderedIds.has(row.id)) return;
        if (query && !String(row.text || "").toLowerCase().includes(query.toLowerCase())) return;
        const empty = bodyEl.querySelector(".clog-empty");
        if (empty) empty.remove();
        let lastKey = null;
        for (let el = bodyEl.lastElementChild; el; el = el.previousElementSibling) {
            if (el.dataset && el.dataset.dateKey) { lastKey = el.dataset.dateKey; break; }
        }
        const nearBottom = bodyEl.scrollHeight - bodyEl.clientHeight - bodyEl.scrollTop < 60;
        const { frag } = buildBlock([row], lastKey);
        bodyEl.appendChild(frag);
        renderedRows += 1;
        if (oldestId == null) oldestId = row.id;
        const added = bodyEl.lastElementChild;
        if (added) {
            added.classList.add("note-new");
            setTimeout(() => added.classList.remove("note-new"), 2000);
        }
        if (nearBottom) { stickBottom = true; stickToBottomFor(400); }
        if (!isMobile) scheduleAutoDismiss(currentDuration);   // keep a new note readable
    }

    async function deleteNote(id, el) {
        if (!confirm("Delete this note?")) return;
        try {
            const res = await fetch(`${base()}/api/notes/${encodeURIComponent(id)}`, {
                method: "DELETE", headers: authHeaders(),
            });
            if (!res.ok && res.status !== 404) throw new Error(`delete -> ${res.status}`);
        } catch (e) {
            console.warn("[notes] delete failed:", e);
            alert("Couldn't delete the note.");
            return;
        }
        renderedIds.delete(id);
        const prev = el.previousElementSibling;
        const next = el.nextElementSibling;
        el.remove();
        // Drop a day separator left with no notes under it.
        if (prev && prev.classList.contains("clog-day-sep")
                && (!next || next.classList.contains("clog-day-sep"))) {
            prev.remove();
        }
        if (!bodyEl.querySelector(".note-row") && !hasMore) setNotice("", emptyText());
    }

    let stickBottom = true;
    function stickToBottomFor(ms) {
        const t0 = performance.now();
        const loop = () => {
            if (!stickBottom) return;
            bodyEl.scrollTop = bodyEl.scrollHeight;
            if (performance.now() - t0 < ms) requestAnimationFrame(loop);
        };
        requestAnimationFrame(loop);
    }

    bodyEl.addEventListener("scroll", () => {
        const dist = bodyEl.scrollHeight - bodyEl.clientHeight - bodyEl.scrollTop;
        stickBottom = dist < 60;
        if (bodyEl.scrollTop < LOAD_THRESHOLD_PX) loadOlder();
    }, { passive: true });

    /* === show / dismiss ================================================== */

    function show(payload) {
        clearTimers();
        clearDismissState("cancelled");
        currentDuration = (payload && payload.duration_s) || 30;
        // Phones share the header with the search field — keep the date short.
        titleEl.textContent = new Date().toLocaleDateString(undefined, isMobile
            ? { weekday: "short", month: "short", day: "numeric" }
            : { weekday: "long", month: "long", day: "numeric" });
        document.body.classList.add(BODY_CLASS);
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
        if (!document.body.classList.contains(BODY_CLASS)) {
            clearDismissState("already-hidden");
            return Promise.resolve(reason);
        }
        if (pendingDismissPromise) return pendingDismissPromise;
        if (document.activeElement === searchEl) searchEl.blur();

        pendingDismissPromise = new Promise((resolve) => {
            pendingDismissResolve = resolve;
            const stage = root;
            const onEnd = (ev) => {
                if (ev && ev.propertyName && ev.propertyName !== "transform") return;
                clearDismissState(reason);
            };
            dismissTransitionListener = onEnd;
            dismissTransitionTarget = stage;
            stage.addEventListener("transitionend", onEnd);
            dismissSafetyTimeout = setTimeout(() => onEnd(null), 1000);
            document.body.classList.remove(BODY_CLASS);
        });
        return pendingDismissPromise;
    }

    function isShown() {
        return document.body.classList.contains(BODY_CLASS);
    }

    return { show, update, add, dismiss, isShown };
}
