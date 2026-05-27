/* K.I.T.T. effect — the iconic red scanner bar at the bottom of the
   screen. A bright crimson light sweeps left-to-right and back, with
   a soft trailing glow on each side, evoking the Knight Industries
   Two Thousand front-grille light. */

export default function setup({ root }) {
    const canvas = root.ownerDocument.createElement("canvas");
    canvas.id = "kitt-fx";
    // Sized to live above the bottom UI row (volume control on the
    // left spans bottom 24-80px; connection indicator on the right
    // also sits at bottom 24px). Strip is centred and pushed well
    // above either, so it never overlaps.
    const HEIGHT_PX = 28;
    Object.assign(canvas.style, {
        position: "absolute",
        left: "50%",
        bottom: "110px",
        transform: "translateX(-50%)",
        width: "min(540px, 60%)",
        height: HEIGHT_PX + "px",
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

    function cssSize() {
        // Read computed CSS size so canvas internal pixels match.
        const rect = canvas.getBoundingClientRect();
        return { w: Math.max(1, Math.round(rect.width)), h: HEIGHT_PX };
    }

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        const { w, h } = cssSize();
        canvas.width = w * dpr;
        canvas.height = h * dpr;
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

        // Faint vertical "cell" dividers — references the segmented
        // look of the original prop without being too literal.
        ctx.fillStyle = "rgba(255, 255, 255, 0.05)";
        const cells = 6;
        for (let i = 1; i < cells; i++) {
            const x = (w / cells) * i;
            ctx.fillRect(x - 0.5, h * 0.25, 1, h * 0.5);
        }
    }

    function drawScanner(w, h) {
        // Position of the bright head along the bar (in px), with a
        // bit of margin so the head doesn't get clipped at the edges.
        const margin = 14;
        const span = w - margin * 2;
        const headX = margin + span * phase;
        const cy = h / 2;

        // The head: a bright radial gradient, scaled to fit the
        // shorter strip height.
        const headR = 14;
        const head = ctx.createRadialGradient(headX, cy, 0, headX, cy, headR);
        head.addColorStop(0, "rgba(255, 220, 200, 1)");
        head.addColorStop(0.18, RED_CORE);
        head.addColorStop(0.55, RED_MID);
        head.addColorStop(1, "rgba(255, 30, 10, 0)");

        // The trailing tail behind the head.
        const tailLen = Math.min(160, span * 0.55);
        const tail = ctx.createLinearGradient(
            direction === 1 ? headX - tailLen : headX + tailLen, cy,
            headX, cy,
        );
        tail.addColorStop(0, RED_TAIL);
        tail.addColorStop(0.55, RED_DIM);
        tail.addColorStop(1, RED_MID);
        ctx.fillStyle = tail;
        const tailX = direction === 1 ? headX - tailLen : headX;
        ctx.fillRect(tailX, cy - 4, tailLen, 8);

        // Bright head glow.
        ctx.fillStyle = head;
        ctx.fillRect(headX - headR * 1.5, cy - headR, headR * 3, headR * 2);

        // Thin solid-red core line for a hard edge inside the housing.
        ctx.fillStyle = "rgba(255, 40, 20, 0.55)";
        ctx.fillRect(margin, cy - 1, span, 2);

        // Soft bloom that bleeds the red glow vertically.
        const bloom = ctx.createRadialGradient(headX, cy, 0, headX, cy, 60);
        bloom.addColorStop(0, "rgba(255, 30, 10, 0.32)");
        bloom.addColorStop(1, "rgba(255, 30, 10, 0)");
        ctx.fillStyle = bloom;
        ctx.fillRect(headX - 60, 0, 120, h);
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

        const { w, h } = cssSize();
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
