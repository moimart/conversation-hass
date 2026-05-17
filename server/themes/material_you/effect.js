/* Material You effect — a slow lava-lamp drift of five soft blobs
   in the Google brand colours (blue, red, yellow, green, plus an
   extra light-blue). The canvas itself gets a heavy CSS blur so
   the blobs blend into ambient washes rather than crisp shapes,
   which is the lava-lamp look but classy enough not to fight the
   birch-wood-and-white-furniture room aesthetic the theme targets.

   Motion: each blob has a slow velocity perturbed by small random
   forces each frame, soft-bounce containment at the edges, and a
   sinusoidal size breathing so it swells and shrinks like real
   lava. Average crossing time is ~30-45 s; nothing snaps. */

export default function setup({ root }) {
    const canvas = root.ownerDocument.createElement("canvas");
    canvas.id = "matyou-fx";
    Object.assign(canvas.style, {
        position: "fixed",
        inset: "0",
        width: "100vw",
        height: "100vh",
        zIndex: "0",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 1.2s ease",
        filter: "blur(56px)",
    });
    root.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    // Google brand palette. Alpha kept moderate — the heavy CSS blur
    // amplifies coverage, and we want the wash to be visible without
    // overwhelming the surface or competing with the orb.
    const COLORS = [
        { core: "rgba( 66, 133, 244, 0.55)", mid: "rgba( 66, 133, 244, 0.16)" },  // Google Blue
        { core: "rgba(234,  67,  53, 0.45)", mid: "rgba(234,  67,  53, 0.14)" },  // Google Red
        { core: "rgba(251, 188,   4, 0.45)", mid: "rgba(251, 188,   4, 0.14)" },  // Google Yellow
        { core: "rgba( 52, 168,  83, 0.45)", mid: "rgba( 52, 168,  83, 0.14)" },  // Google Green
        { core: "rgba(132, 168, 235, 0.40)", mid: "rgba(132, 168, 235, 0.12)" },  // Soft secondary blue
    ];

    let blobs = [];
    let raf = null;
    let onResize = null;
    let lastFrame = 0;

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = window.innerWidth * dpr;
        canvas.height = window.innerHeight * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function rand(min, max) { return min + Math.random() * (max - min); }

    function spawn() {
        const w = window.innerWidth, h = window.innerHeight;
        // Blob radius scales with the smaller viewport dimension so
        // the wash looks proportional on both a kiosk and a phone.
        const base = Math.min(w, h);
        blobs = COLORS.map((c) => ({
            x: rand(0, w),
            y: rand(0, h),
            vx: rand(-8, 8),                       // px/sec — very slow
            vy: rand(-8, 8),
            r: rand(base * 0.35, base * 0.55),     // huge, soft
            phase: rand(0, Math.PI * 2),
            color: c,
        }));
    }

    function tick(now) {
        if (raf === null) return;
        const dt = Math.min(0.1, (now - (lastFrame || now)) / 1000);  // clamp huge frame gaps
        lastFrame = now;
        const w = window.innerWidth, h = window.innerHeight;
        ctx.clearRect(0, 0, w, h);

        for (const b of blobs) {
            // Tiny random perturbation each frame so the motion never
            // feels mechanical or quite periodic.
            b.vx += rand(-2.5, 2.5) * dt;
            b.vy += rand(-2.5, 2.5) * dt;

            // Soft speed cap — keeps everything lava-slow.
            const speed = Math.hypot(b.vx, b.vy);
            const maxV = 14;
            if (speed > maxV) { b.vx *= maxV / speed; b.vy *= maxV / speed; }

            b.x += b.vx * dt;
            b.y += b.vy * dt;

            // Soft containment: allow the blob to extend a bit off-screen
            // (the blur smears it nicely off the edge), then push it
            // back in. Damping the bounce stops jitter at corners.
            const margin = b.r * 0.4;
            if (b.x < -margin) { b.x = -margin; b.vx = Math.abs(b.vx) * 0.9; }
            if (b.x > w + margin) { b.x = w + margin; b.vx = -Math.abs(b.vx) * 0.9; }
            if (b.y < -margin) { b.y = -margin; b.vy = Math.abs(b.vy) * 0.9; }
            if (b.y > h + margin) { b.y = h + margin; b.vy = -Math.abs(b.vy) * 0.9; }

            // Size "breathing" — ±12% over ~14 s, phase-offset per blob.
            const r = b.r * (1 + 0.12 * Math.sin(now * 0.00045 + b.phase));

            const g = ctx.createRadialGradient(b.x, b.y, 0, b.x, b.y, r);
            g.addColorStop(0, b.color.core);
            g.addColorStop(0.55, b.color.mid);
            g.addColorStop(1, "rgba(0, 0, 0, 0)");
            ctx.fillStyle = g;
            ctx.beginPath();
            ctx.arc(b.x, b.y, r, 0, Math.PI * 2);
            ctx.fill();
        }

        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            spawn();
            requestAnimationFrame(() => { canvas.style.opacity = "0.8"; });
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
            blobs = [];
        },
    };
}
