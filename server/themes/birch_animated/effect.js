/* Birch — Animated ambient effect.
   ---------------------------------
   Stylised birch leaves (small almond shapes with a subtle midrib)
   tumbling gently downward across the page. Each leaf has its own
   slow rotation, a sinusoidal horizontal sway, and one of four warm
   palette colours. Japandi-elegant: clean shapes, low count, gentle
   motion — the page should feel like it's autumn through a sunlit
   window.

   Canvas-based so we can draw real shapes (ellipses + midrib lines)
   with rotation per particle without going wild on DOM nodes. */

export default function setup({ root }) {
    const canvas = root.ownerDocument.createElement("canvas");
    canvas.id = "birch-animated-fx";
    Object.assign(canvas.style, {
        position: "fixed",
        inset: "0",
        width: "100vw",
        height: "100vh",
        zIndex: "0",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 1.2s ease",
    });
    root.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    // Warm autumn-birch palette. Each pair is [fill, midrib] so the
    // midrib is a slightly darker tone of the same hue — keeps the
    // leaf legible without looking flat.
    const PALETTE = [
        { fill: "rgba(217, 122,  53, 0.85)", rib: "rgba(140,  70,  20, 0.55)" },  // amber
        { fill: "rgba(201, 110,  31, 0.82)", rib: "rgba(120,  60,  15, 0.55)" },  // copper
        { fill: "rgba(232, 165, 100, 0.80)", rib: "rgba(150,  90,  40, 0.50)" },  // soft bronze
        { fill: "rgba(255, 232, 184, 0.78)", rib: "rgba(165, 120,  60, 0.45)" },  // cream
        { fill: "rgba(178,  90,  28, 0.85)", rib: "rgba(100,  50,  10, 0.55)" },  // deep copper
    ];

    let leaves = [];
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

    function spawnLeaf(seedAnywhere) {
        // Leaves spawn off the top and drift downward; on first frame
        // we seed across the viewport so the page is populated.
        const size = rand(14, 26);   // half-length along the leaf's long axis
        return {
            x: rand(-40, window.innerWidth + 40),
            y: seedAnywhere ? rand(0, window.innerHeight) : rand(-120, -20),
            size,                              // long-axis half-length
            ratio: rand(0.34, 0.46),           // short/long ratio (leaf "width")
            rot: rand(0, Math.PI * 2),         // current rotation
            rotSpeed: rand(-0.35, 0.35),       // rad/sec
            vy: rand(24, 46),                  // downward fall speed, px/sec
            swayAmp: rand(14, 36),             // horizontal sway amplitude
            swaySpeed: rand(0.0004, 0.0011),
            phase: rand(0, Math.PI * 2),
            color: PALETTE[Math.floor(Math.random() * PALETTE.length)],
            alpha: rand(0.7, 1.0),
        };
    }

    function ensureCount() {
        // Conservative count — leaves are bigger than the old blobs so
        // we don't need many to feel populated.
        const target = Math.min(22, Math.max(10, Math.floor(window.innerWidth * window.innerHeight / 90000)));
        while (leaves.length < target) leaves.push(spawnLeaf(false));
    }

    function drawLeaf(l) {
        const longR = l.size;
        const shortR = l.size * l.ratio;
        ctx.save();
        ctx.translate(l.x, l.y);
        ctx.rotate(l.rot);
        ctx.globalAlpha = l.alpha;

        // Leaf body — a softly-shaded ellipse. A subtle radial gradient
        // gives a "lit from above" depth so leaves don't read as flat
        // discs.
        const grad = ctx.createRadialGradient(0, -shortR * 0.2, 0, 0, 0, longR);
        grad.addColorStop(0, l.color.fill);
        // Same hue but a bit deeper at the edges:
        grad.addColorStop(1, l.color.fill.replace(/0\.\d+\)/, "0.45)"));
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.ellipse(0, 0, longR, shortR, 0, 0, Math.PI * 2);
        ctx.fill();

        // Midrib — a single thin line down the long axis. This is what
        // makes the shape read as "leaf" rather than "lozenge".
        ctx.strokeStyle = l.color.rib;
        ctx.lineWidth = Math.max(0.8, l.size * 0.06);
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(-longR * 0.85, 0);
        ctx.lineTo(longR * 0.85, 0);
        ctx.stroke();

        // Two tiny side veins for elegance — only on larger leaves so
        // small ones stay clean.
        if (l.size > 18) {
            ctx.lineWidth = Math.max(0.5, l.size * 0.035);
            ctx.beginPath();
            ctx.moveTo(-longR * 0.2, 0);
            ctx.lineTo(-longR * 0.05, -shortR * 0.55);
            ctx.moveTo( longR * 0.05, 0);
            ctx.lineTo( longR * 0.30,  shortR * 0.55);
            ctx.stroke();
        }

        ctx.restore();
        ctx.globalAlpha = 1;
    }

    function tick(now) {
        if (raf === null) return;
        const dt = (now - (lastFrame || now)) / 1000;
        lastFrame = now;
        ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
        ensureCount();
        for (const l of leaves) {
            l.y += l.vy * dt;
            l.x += Math.sin(now * l.swaySpeed + l.phase) * l.swayAmp * dt * 0.35;
            l.rot += l.rotSpeed * dt;
            drawLeaf(l);
        }
        // Recycle leaves that have fallen past the bottom.
        leaves = leaves.filter(l => l.y - l.size * 2 < window.innerHeight + 40);
        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            leaves = [];
            for (let i = 0; i < 16; i++) leaves.push(spawnLeaf(true));
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
            leaves = [];
        },
    };
}
