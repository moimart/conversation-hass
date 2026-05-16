/* K.I.T.T. effect — the iconic red scanner bar at the bottom of the
   screen. A bright crimson light sweeps left-to-right and back, with
   a soft trailing glow on each side, evoking the Knight Industries
   Two Thousand front-grille light. */

export default function setup({ root }) {
    const canvas = root.ownerDocument.createElement("canvas");
    canvas.id = "kitt-fx";
    Object.assign(canvas.style, {
        position: "fixed",
        left: "0",
        right: "0",
        bottom: "0",
        width: "100vw",
        height: "64px",
        zIndex: "5",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 0.6s ease",
    });
    root.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    let raf = null;
    let onResize = null;
    let lastFrame = 0;
    let phase = 0;            // 0..1, position along the bar
    let direction = 1;        // 1 = rightward, -1 = leftward
    const PERIOD_MS = 1600;   // one full sweep one direction

    const RED_CORE = "rgba(255, 60, 30, 1)";
    const RED_MID = "rgba(255, 30, 10, 0.85)";
    const RED_DIM = "rgba(180, 0, 0, 0.55)";
    const RED_TAIL = "rgba(120, 0, 0, 0.0)";

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = window.innerWidth * dpr;
        canvas.height = 64 * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function drawBackdrop(w, h) {
        // A thin chrome housing strip — dark with a subtle highlight,
        // so the scanner reads as embedded in a bezel rather than just
        // floating over the page.
        const housing = ctx.createLinearGradient(0, 0, 0, h);
        housing.addColorStop(0, "rgba(0, 0, 0, 0.0)");
        housing.addColorStop(0.25, "rgba(8, 8, 8, 0.65)");
        housing.addColorStop(0.5, "rgba(20, 20, 20, 0.85)");
        housing.addColorStop(0.75, "rgba(8, 8, 8, 0.65)");
        housing.addColorStop(1, "rgba(0, 0, 0, 0.0)");
        ctx.fillStyle = housing;
        ctx.fillRect(0, 0, w, h);

        // Eight faint vertical "cell" dividers — references the
        // segmented look of the original prop without being too literal.
        ctx.fillStyle = "rgba(255, 255, 255, 0.04)";
        const cells = 8;
        for (let i = 1; i < cells; i++) {
            const x = (w / cells) * i;
            ctx.fillRect(x - 0.5, h * 0.25, 1, h * 0.5);
        }
    }

    function drawScanner(w, h) {
        // Position of the bright head along the bar (in px), with a
        // bit of margin so the head doesn't get clipped at the edges.
        const margin = 24;
        const span = w - margin * 2;
        const headX = margin + span * phase;
        const cy = h / 2;

        // The head: a very bright radial gradient.
        const headR = 28;
        const head = ctx.createRadialGradient(headX, cy, 0, headX, cy, headR);
        head.addColorStop(0, "rgba(255, 220, 200, 1)");
        head.addColorStop(0.18, RED_CORE);
        head.addColorStop(0.55, RED_MID);
        head.addColorStop(1, "rgba(255, 30, 10, 0)");

        // The trailing tail behind the head — a horizontal gradient
        // pointing opposite the direction of travel.
        const tailLen = 240;
        const tailStart = headX - direction * tailLen;
        const tail = ctx.createLinearGradient(tailStart, cy, headX, cy);
        tail.addColorStop(0, RED_TAIL);
        tail.addColorStop(0.55, RED_DIM);
        tail.addColorStop(1, RED_MID);

        // Draw tail first (a thinner band) so the head sits on top.
        ctx.fillStyle = tail;
        const tailX = direction === 1 ? headX - tailLen : headX;
        ctx.fillRect(tailX, cy - 8, tailLen, 16);

        // Draw the bright head glow.
        ctx.fillStyle = head;
        ctx.fillRect(headX - headR * 1.5, cy - headR, headR * 3, headR * 2);

        // A thin solid-red core line inside the housing for a hard edge.
        ctx.fillStyle = "rgba(255, 40, 20, 0.55)";
        ctx.fillRect(margin, cy - 1.5, span, 3);

        // Soft outer bloom under the housing — bleeds the red glow
        // upward into the page a little.
        const bloom = ctx.createRadialGradient(headX, cy, 0, headX, cy, 120);
        bloom.addColorStop(0, "rgba(255, 30, 10, 0.35)");
        bloom.addColorStop(1, "rgba(255, 30, 10, 0)");
        ctx.fillStyle = bloom;
        ctx.fillRect(headX - 120, 0, 240, h);
    }

    function tick(now) {
        if (raf === null) return;
        const dt = now - (lastFrame || now);
        lastFrame = now;

        // Advance phase. One PERIOD_MS = travel one way; reverse and
        // continue the other way. Use a small ease near the ends so the
        // light decelerates and re-accelerates instead of snapping back.
        phase += direction * (dt / PERIOD_MS);
        if (phase >= 1) {
            phase = 1 - (phase - 1);
            direction = -1;
        } else if (phase <= 0) {
            phase = -phase;
            direction = 1;
        }

        const w = window.innerWidth;
        const h = 64;
        ctx.clearRect(0, 0, w, h);
        drawBackdrop(w, h);
        drawScanner(w, h);

        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            phase = 0;
            direction = 1;
            requestAnimationFrame(() => { canvas.style.opacity = "1"; });
            lastFrame = performance.now();
            raf = requestAnimationFrame(tick);
        },
        stop() {
            if (raf !== null) cancelAnimationFrame(raf);
            raf = null;
            if (onResize) {
                window.removeEventListener("resize", onResize);
                onResize = null;
            }
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            canvas.style.opacity = "0";
        },
    };
}
