/* Calendar overlay — month / week / day grids rendered onto the
   #calendar-root element. Driven by show_calendar / hide_calendar
   messages from the server.

   Public API: mountCalendar(root) -> { show(payload), update(payload),
   dismiss(reason), isShown() }. dismiss() returns a Promise that
   resolves when the cube has finished rotating back, used by app.js
   to queue camera/video/image takeovers cleanly. */

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function mountCalendar(root) {
    if (!root) throw new Error("mountCalendar: root is required");

    let dismissTimer = null;
    let countdownStart = 0;
    let countdownDuration = 0;
    let countdownRaf = null;
    let currentPayload = null;
    let pendingDismissPromise = null;
    let pendingDismissResolve = null;
    let dismissSafetyTimeout = null;
    let dismissTransitionListener = null;
    let dismissTransitionTarget = null;

    // Build the static overlay shell once. show()/update() repaint .cal-body.
    root.innerHTML = "";
    const overlay = document.createElement("div");
    overlay.className = "calendar-overlay";
    overlay.innerHTML = `
        <div class="cal-header">
          <div class="cal-header-titles">
            <div class="cal-source"></div>
            <div class="cal-title"></div>
          </div>
          <div class="cal-status-mirror">
            <span class="cal-status-dot"></span>
            <span class="cal-status-label">IDLE</span>
          </div>
          <div class="cal-countdown-bar"></div>
        </div>
        <div class="cal-body"></div>
    `;
    root.appendChild(overlay);

    const titleEl = overlay.querySelector(".cal-title");
    const sourceEl = overlay.querySelector(".cal-source");
    const bodyEl = overlay.querySelector(".cal-body");
    const countdownEl = overlay.querySelector(".cal-countdown-bar");
    const statusLabelEl = overlay.querySelector(".cal-status-label");

    // Mirror body.state-* into the calendar status label text. The dot
    // colour is purely CSS-driven from body.state-* (see calendar.css).
    function refreshStatusLabel() {
        const cls = document.body.className.split(/\s+/);
        let label = "IDLE";
        for (const c of cls) {
            if (c === "state-listening")  { label = "LISTENING";  break; }
            if (c === "state-processing") { label = "PROCESSING"; break; }
            if (c === "state-speaking")   { label = "SPEAKING";   break; }
        }
        statusLabelEl.textContent = label;
    }
    const stateObserver = new MutationObserver(refreshStatusLabel);
    stateObserver.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    refreshStatusLabel();

    function clearTimers() {
        if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }
        if (countdownRaf) { cancelAnimationFrame(countdownRaf); countdownRaf = null; }
    }

    /* Clear any in-flight dismiss state (called from show() to cancel
       a dismiss that's mid-flight, and from the dismiss completion
       handler itself). Guarantees that pendingDismissPromise and
       pendingDismissResolve are always cleared together so the
       dismiss() guard `if (pendingDismissPromise) return ...` never
       returns a stale-and-resolved promise. */
    function clearDismissState(reason) {
        if (dismissSafetyTimeout) {
            clearTimeout(dismissSafetyTimeout);
            dismissSafetyTimeout = null;
        }
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
        if (dismissTimer) clearTimeout(dismissTimer);
        if (countdownRaf) cancelAnimationFrame(countdownRaf);
        startCountdown(durationS);
        dismissTimer = setTimeout(() => dismiss("timeout"), durationS * 1000);
    }

    function render(payload, opts = {}) {
        currentPayload = payload;
        sourceEl.textContent = payload.source_label || "";
        titleEl.textContent  = payload.title || "";
        bodyEl.innerHTML = "";
        const view = (payload.view || "month").toLowerCase();
        try {
            if (view === "week")      renderWeek(bodyEl, payload);
            else if (view === "day")  renderDay(bodyEl, payload);
            else                      renderMonth(bodyEl, payload);
        } catch (e) {
            console.error("[calendar] render error:", e);
            bodyEl.innerHTML = `<div class="cal-error">Calendar render failed: ${escapeHtml(String(e))}</div>`;
        }
        if (opts.fadeSwap) {
            bodyEl.classList.remove("fade-swap");
            // Force reflow then re-add for animation restart.
            void bodyEl.offsetWidth;
            bodyEl.classList.add("fade-swap");
        }
    }

    function show(payload) {
        // Cancel any pending dismiss (we're showing fresh content).
        clearTimers();
        clearDismissState("cancelled");
        render(payload);
        document.body.classList.add("show-calendar");
        scheduleAutoDismiss(payload.duration_s || 30);
    }

    function update(payload) {
        // Re-invocation while shown. If the view changed, fade-swap the body.
        const viewChanged = !currentPayload || currentPayload.view !== payload.view;
        render(payload, { fadeSwap: viewChanged });
        scheduleAutoDismiss(payload.duration_s || 30);
    }

    function dismiss(reason = "explicit") {
        clearTimers();
        if (!document.body.classList.contains("show-calendar")) {
            // Already hidden — clear any stale dismiss state and resolve.
            clearDismissState("already-hidden");
            return Promise.resolve(reason);
        }
        if (pendingDismissPromise) return pendingDismissPromise;

        pendingDismissPromise = new Promise((resolve) => {
            pendingDismissResolve = resolve;
            // Listen on .calendar-stage — that's the element whose
            // transform actually transitions back to rotateY(90deg).
            // (#app no longer has any transition.)
            const stage = document.querySelector(".calendar-stage");
            const onEnd = (ev) => {
                if (ev && ev.propertyName && ev.propertyName !== "transform") return;
                clearDismissState(reason);
            };
            dismissTransitionListener = onEnd;
            dismissTransitionTarget = stage;
            if (stage) stage.addEventListener("transitionend", onEnd);
            // Safety: resolve after 1s if transitionend doesn't fire
            // (slow GPU, no transition triggered, etc.).
            dismissSafetyTimeout = setTimeout(() => onEnd(null), 1000);
            document.body.classList.remove("show-calendar");
        });
        return pendingDismissPromise;
    }

    function isShown() {
        return document.body.classList.contains("show-calendar");
    }

    return { show, update, dismiss, isShown };
}

/* === MONTH ============================================================== */

function renderMonth(root, payload) {
    const { start, end } = payload.range || {};
    const events = payload.events || [];
    const monthStart = new Date(start);
    const today = new Date(); today.setHours(0, 0, 0, 0);

    // First Monday on/before the 1st of the month
    const gridStart = new Date(monthStart);
    const dow = (gridStart.getDay() + 6) % 7;     // 0 = Mon
    gridStart.setDate(gridStart.getDate() - dow);

    // 6 rows × 7 cols = 42 cells
    const cells = [];
    for (let i = 0; i < 42; i++) {
        const d = new Date(gridStart);
        d.setDate(gridStart.getDate() + i);
        cells.push(d);
    }

    const grid = document.createElement("div");
    grid.className = "cal-month-grid";

    const wkHeader = document.createElement("div");
    wkHeader.className = "cal-weekday-header";
    for (const wd of WEEKDAYS) {
        const h = document.createElement("div");
        h.className = "cal-weekday";
        h.textContent = wd;
        wkHeader.appendChild(h);
    }
    grid.appendChild(wkHeader);

    // Index events per day for single-day rendering. Multi-day spanning
    // pills are computed per week-row separately.
    const sameDayEvents = new Map();           // YYYY-MM-DD -> [event]
    const multiDayEvents = [];                 // {start: Date, end: Date, ev}

    for (const ev of events) {
        const evStart = parseEventDate(ev.start, ev.all_day);
        const evEnd = parseEventDate(ev.end, ev.all_day);
        if (!evStart) continue;
        const span = !ev.all_day
            ? sameDay(evStart, evEnd)
            : isSameDayAllDay(evStart, evEnd);
        if (span) {
            const key = ymd(evStart);
            if (!sameDayEvents.has(key)) sameDayEvents.set(key, []);
            sameDayEvents.get(key).push({ ev, evStart, evEnd });
        } else {
            multiDayEvents.push({ ev, evStart, evEnd });
        }
    }

    // Build cells
    const cellEls = [];
    for (let i = 0; i < 42; i++) {
        const d = cells[i];
        const cellEl = document.createElement("div");
        cellEl.className = "cal-day-cell";
        if (d.getMonth() !== monthStart.getMonth()) cellEl.classList.add("other-month");
        if (sameDate(d, today)) cellEl.classList.add("today");

        const num = document.createElement("div");
        num.className = "cal-day-num";
        num.textContent = d.getDate();
        cellEl.appendChild(num);

        const evContainer = document.createElement("div");
        evContainer.className = "cal-day-events";
        cellEl.appendChild(evContainer);

        cellEls.push({ cellEl, evContainer, date: d });
        grid.appendChild(cellEl);
    }

    // Pour single-day events into their cells (cap at 3 visible, +N more).
    for (const { cellEl, evContainer, date } of cellEls) {
        const list = sameDayEvents.get(ymd(date)) || [];
        list.sort((a, b) => a.evStart - b.evStart);
        const max = 3;
        for (let i = 0; i < Math.min(list.length, max); i++) {
            evContainer.appendChild(buildEventPill(list[i].ev, list[i].evStart));
        }
        if (list.length > max) {
            const overflow = document.createElement("div");
            overflow.className = "cal-overflow";
            overflow.textContent = `+${list.length - max} more`;
            evContainer.appendChild(overflow);
        }
    }

    // Multi-day spanning pills, per week-row. For each week (6 rows),
    // for each event that overlaps it, paint one pill across the
    // intersecting cells.
    for (let row = 0; row < 6; row++) {
        const rowStart = cells[row * 7];
        const rowEnd = new Date(rowStart);
        rowEnd.setDate(rowStart.getDate() + 7);          // exclusive
        for (const { ev, evStart, evEnd } of multiDayEvents) {
            const inclusiveEnd = ev.all_day
                ? new Date(evEnd.getFullYear(), evEnd.getMonth(), evEnd.getDate())  // HA all-day end is exclusive
                : evEnd;
            if (inclusiveEnd <= rowStart || evStart >= rowEnd) continue;
            const segStart = evStart < rowStart ? rowStart : evStart;
            const segEndExclusive = inclusiveEnd > rowEnd ? rowEnd : inclusiveEnd;
            const startCol = Math.max(0, daysBetween(rowStart, segStart));
            const endCol   = Math.min(6, daysBetween(rowStart, segEndExclusive) - 1);
            const isStart = segStart.getTime() === evStart.getTime();
            const isEnd   = segEndExclusive.getTime() === inclusiveEnd.getTime();
            paintSpanningPill(cellEls, row, startCol, endCol, ev, isStart, isEnd);
        }
    }

    if (events.length === 0) {
        const empty = document.createElement("div");
        empty.className = "cal-empty";
        empty.textContent = "No events this month.";
        root.appendChild(empty);
        return;
    }
    root.appendChild(grid);
}

function paintSpanningPill(cellEls, row, startCol, endCol, ev, isStart, isEnd) {
    for (let col = startCol; col <= endCol; col++) {
        const idx = row * 7 + col;
        if (idx >= cellEls.length) break;
        const { evContainer } = cellEls[idx];
        const pill = document.createElement("div");
        pill.className = "cal-event-pill spanning cal-c" + (ev.color_idx % 6);
        if (col === startCol && isStart) pill.classList.add("start");
        if (col === endCol && isEnd) pill.classList.add("end");
        pill.textContent = col === startCol ? ev.summary : "";
        pill.title = ev.summary;
        evContainer.appendChild(pill);
    }
}

/* === WEEK =============================================================== */

function renderWeek(root, payload) {
    const { start } = payload.range || {};
    const events = payload.events || [];
    const weekStart = new Date(start);
    const today = new Date(); today.setHours(0, 0, 0, 0);

    const grid = document.createElement("div");
    grid.className = "cal-week-grid";

    for (let i = 0; i < 7; i++) {
        const d = new Date(weekStart);
        d.setDate(weekStart.getDate() + i);
        const day = document.createElement("div");
        day.className = "cal-week-day";
        if (sameDate(d, today)) day.classList.add("today");

        const header = document.createElement("div");
        header.className = "cal-week-day-header";
        header.textContent = WEEKDAYS[i];
        day.appendChild(header);

        const num = document.createElement("div");
        num.className = "cal-week-day-num";
        num.textContent = d.getDate();
        day.appendChild(num);

        const list = document.createElement("div");
        list.className = "cal-week-day-events";
        day.appendChild(list);

        const dayEvents = events
            .map((ev) => ({ ev, s: parseEventDate(ev.start, ev.all_day), e: parseEventDate(ev.end, ev.all_day) }))
            .filter(({ s }) => s && sameDate(s, d))
            .sort((a, b) => a.s - b.s);

        for (const { ev, s } of dayEvents) {
            const item = document.createElement("div");
            item.className = "cal-week-event cal-c" + (ev.color_idx % 6);
            if (!ev.all_day) {
                const time = document.createElement("div");
                time.className = "cal-week-event-time";
                time.textContent = formatTime(s);
                item.appendChild(time);
            }
            const summary = document.createElement("div");
            summary.textContent = ev.summary;
            item.appendChild(summary);
            list.appendChild(item);
        }

        grid.appendChild(day);
    }

    if (events.length === 0) {
        const empty = document.createElement("div");
        empty.className = "cal-empty";
        empty.textContent = "No events this week.";
        root.appendChild(empty);
        return;
    }
    root.appendChild(grid);
}

/* === DAY ================================================================
   Continuous timeline: every event is absolutely positioned so its top
   matches the start time and its height matches the duration. Overlapping
   events get laid out side-by-side, Google-Cal style, by greedy-assigning
   each event to the lowest-numbered track free at its start and then
   splitting the available width across tracks within each connected
   overlap group. Auto-fit density: try to fit 24 h, but if the available
   container height implies hours smaller than ~24 px, force a minimum
   hour height and let the timeline scroll. */

function renderDay(root, payload) {
    const { start } = payload.range || {};
    const events = payload.events || [];
    const day = new Date(start);

    if (events.length === 0) {
        const empty = document.createElement("div");
        empty.className = "cal-empty";
        empty.textContent = "No events today.";
        root.appendChild(empty);
        return;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "cal-day-wrapper";

    // All-day events sit in a separate strip at the top.
    const allDay = events.filter((ev) => ev.all_day);
    if (allDay.length > 0) {
        const strip = document.createElement("div");
        strip.className = "cal-day-allday";
        for (const ev of allDay) {
            const item = document.createElement("div");
            item.className = "cal-day-event cal-c" + (ev.color_idx % 6);
            const t = document.createElement("div");
            t.className = "cal-day-event-time";
            t.textContent = "ALL DAY";
            item.appendChild(t);
            const s = document.createElement("div");
            s.textContent = ev.summary;
            item.appendChild(s);
            strip.appendChild(item);
        }
        wrapper.appendChild(strip);
    }

    // Build the absolutely-positioned timeline. Height = 24h * hourPx;
    // the scroll container picks min-height when content fits.
    const timeline = document.createElement("div");
    timeline.className = "cal-day-timeline";
    wrapper.appendChild(timeline);
    root.appendChild(wrapper);

    // We need the wrapper to be in the DOM to measure its height for
    // auto-fit. Compute the height of one hour: prefer fitting all 24 h,
    // but enforce a minimum so events stay readable.
    const MIN_HOUR_PX = 26;
    const allDayPx = allDay.length > 0 ? 56 : 0;
    const availPx = Math.max(0, (wrapper.parentElement?.clientHeight || 600) - allDayPx);
    const hourPx = Math.max(MIN_HOUR_PX, Math.floor(availPx / 24));
    const timelineHeight = hourPx * 24;
    timeline.style.height = timelineHeight + "px";
    timeline.style.setProperty("--cal-hour-px", hourPx + "px");

    // Hour grid lines + labels (every hour gets a horizontal divider and
    // a label in the left gutter).
    for (let h = 0; h < 24; h++) {
        const top = h * hourPx;
        const line = document.createElement("div");
        line.className = "cal-hour-line";
        line.style.top = top + "px";
        timeline.appendChild(line);

        const label = document.createElement("div");
        label.className = "cal-hour-label-abs";
        label.style.top = (top - 6) + "px";
        label.textContent = String(h).padStart(2, "0") + ":00";
        timeline.appendChild(label);
    }

    // Compute event boxes with track assignment.
    const items = events
        .filter((ev) => !ev.all_day)
        .map((ev) => {
            const s = parseEventDate(ev.start, false);
            const e = parseEventDate(ev.end, false);
            if (!s || !sameDate(s, day)) return null;
            const startMin = s.getHours() * 60 + s.getMinutes();
            let endMin;
            if (!e || !sameDate(e, day)) {
                // Event continues past midnight — clamp to 24:00.
                endMin = 24 * 60;
            } else {
                endMin = e.getHours() * 60 + e.getMinutes();
            }
            // Minimum visual duration so a 5-minute event is still tappable.
            const effectiveEnd = Math.max(endMin, startMin + 15);
            return { ev, s, e, startMin, endMin, effectiveEnd };
        })
        .filter(Boolean)
        .sort((a, b) => a.startMin - b.startMin || a.effectiveEnd - b.effectiveEnd);

    // Greedy track assignment: lowest-numbered track whose last event
    // ends at or before this event's start.
    const trackEnds = [];   // index = track #, value = effectiveEnd of latest event in it
    for (const it of items) {
        let track = 0;
        while (track < trackEnds.length && trackEnds[track] > it.startMin) track++;
        if (track === trackEnds.length) trackEnds.push(it.effectiveEnd);
        else trackEnds[track] = it.effectiveEnd;
        it.track = track;
    }

    // Connected overlap groups: events whose intervals form a chain of
    // overlaps share a numCols, so width-splitting is consistent across
    // the group instead of recomputed per-event.
    let groupStart = null;
    let groupEnd = -1;
    let groupItems = [];
    const flushGroup = () => {
        if (groupItems.length === 0) return;
        const numCols = Math.max(...groupItems.map((it) => it.track)) + 1;
        for (const it of groupItems) it.numCols = numCols;
        groupItems = [];
    };
    for (const it of items) {
        if (groupStart === null || it.startMin >= groupEnd) {
            flushGroup();
            groupStart = it.startMin;
            groupEnd = it.effectiveEnd;
            groupItems = [it];
        } else {
            groupItems.push(it);
            groupEnd = Math.max(groupEnd, it.effectiveEnd);
        }
    }
    flushGroup();

    // Paint.
    for (const it of items) {
        const box = document.createElement("div");
        box.className = "cal-day-event cal-c" + (it.ev.color_idx % 6);
        const top = (it.startMin / 60) * hourPx;
        const height = ((it.effectiveEnd - it.startMin) / 60) * hourPx;
        box.style.top = top + "px";
        box.style.height = Math.max(18, height) + "px";
        // Width: split among numCols, leaving a 2 px gutter between adjacent.
        const widthPct = 100 / (it.numCols || 1);
        const leftPct = it.track * widthPct;
        box.style.left = `calc(${leftPct}% + 2px)`;
        box.style.width = `calc(${widthPct}% - 4px)`;
        box.title = `${it.ev.summary} — ${formatTime(it.s)}${it.e ? "–" + formatTime(it.e) : ""}`;

        // Compact label: time on first line, summary on the next.
        const t = document.createElement("div");
        t.className = "cal-day-event-time";
        t.textContent = formatTime(it.s);
        box.appendChild(t);
        const summary = document.createElement("div");
        summary.className = "cal-day-event-title";
        summary.textContent = it.ev.summary;
        box.appendChild(summary);

        timeline.appendChild(box);
    }
}

/* === Helpers ============================================================ */

function buildEventPill(ev, evStart) {
    const pill = document.createElement("div");
    pill.className = "cal-event-pill cal-c" + (ev.color_idx % 6);
    pill.title = ev.summary;
    let text = ev.summary;
    if (!ev.all_day && evStart) text = `${formatTime(evStart)} ${ev.summary}`;
    pill.textContent = text;
    return pill;
}

function parseEventDate(s, allDay) {
    if (!s) return null;
    if (allDay && s.length === 10) {
        // YYYY-MM-DD, treat as local midnight
        const [y, m, d] = s.split("-").map(Number);
        return new Date(y, m - 1, d);
    }
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
}

function sameDate(a, b) {
    return a && b && a.getFullYear() === b.getFullYear()
        && a.getMonth() === b.getMonth()
        && a.getDate() === b.getDate();
}

function sameDay(a, b) {
    return sameDate(a, b);
}

function isSameDayAllDay(start, end) {
    if (!start || !end) return true;
    // HA all-day ranges are end-exclusive; same-day means end - start == 1 day.
    const diffDays = Math.round((end - start) / 86400000);
    return diffDays <= 1;
}

function ymd(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

function daysBetween(a, b) {
    return Math.floor((b - a) / 86400000);
}

function formatTime(d) {
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
}
