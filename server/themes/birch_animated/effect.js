/* Birch — Animated ambient effect.
   ---------------------------------
   Stylised birch leaves (small almond shapes with a midrib) tumbling
   gently downward. Japandi-elegant: clean shapes, low count, slow
   motion.

   RPi performance plan:
     - Each unique leaf colour is rasterised ONCE into an offscreen
       canvas (sprite) at start(). Per-frame work is just a drawImage
       with rotation — no per-frame gradient creation, no ellipse
       fill, no stroke calls.
     - Frame rate capped at 30 fps. Drift is slow enough that 30 fps
       looks identical to 60 to the eye, halves CPU/GPU cost.
     - Leaf count is conservative (≤16 on a 1080p kiosk). */

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

    // Warm autumn-birch palette. Each entry is the fill colour + a
    // darker matching midrib.
    const PALETTE = [
        { fill: "rgba(217, 122,  53, 0.90)", rib: "rgba(140,  70,  20, 0.55)" },  // amber
        { fill: "rgba(201, 110,  31, 0.88)", rib: "rgba(120,  60,  15, 0.55)" },  // copper
        { fill: "rgba(232, 165, 100, 0.85)", rib: "rgba(150,  90,  40, 0.50)" },  // soft bronze
        { fill: "rgba(255, 232, 184, 0.85)", rib: "rgba(165, 120,  60, 0.45)" },  // cream
        { fill: "rgba(178,  90,  28, 0.90)", rib: "rgba(100,  50,  10, 0.55)" },  // deep copper
    ];

    // Sprite reference size: the largest leaf we'll ever draw. Smaller
    // leaves are produced by drawing the sprite at < 1.0 scale.
    const SPRITE_LONG = 28;
    const SPRITE_SHORT_RATIO = 0.40;
    const SPRITE_PAD = 6;                         // breathing room for the rib line
    const SPRITE_SIZE = (SPRITE_LONG + SPRITE_PAD) * 2;

    /** One offscreen canvas per palette entry. Built once. */
    const sprites = PALETTE.map((c) => {
        const off = root.ownerDocument.createElement("canvas");
        off.width = SPRITE_SIZE;
        off.height = SPRITE_SIZE;
        const o = off.getContext("2d");
        const cx = SPRITE_SIZE / 2;
        const cy = SPRITE_SIZE / 2;
        const longR = SPRITE_LONG;
        const shortR = SPRITE_LONG * SPRITE_SHORT_RATIO;

        // Leaf body — gradient baked once into the sprite.
        const grad = o.createRadialGradient(cx, cy - shortR * 0.2, 0, cx, cy, longR);
        grad.addColorStop(0, c.fill);
        grad.addColorStop(1, c.fill.replace(/0\.\d+\)/, "0.45)"));
        o.fillStyle = grad;
        o.beginPath();
        o.ellipse(cx, cy, longR, shortR, 0, 0, Math.PI * 2);
        o.fill();

        // Midrib + side veins, also baked.
        o.strokeStyle = c.rib;
        o.lineCap = "round";
        o.lineWidth = Math.max(0.9, SPRITE_LONG * 0.06);
        o.beginPath();
        o.moveTo(cx - longR * 0.85, cy);
        o.lineTo(cx + longR * 0.85, cy);
        o.stroke();
        o.lineWidth = Math.max(0.5, SPRITE_LONG * 0.035);
        o.beginPath();
        o.moveTo(cx - longR * 0.2, cy);
        o.lineTo(cx - longR * 0.05, cy - shortR * 0.55);
        o.moveTo(cx + longR * 0.05, cy);
        o.lineTo(cx + longR * 0.30, cy + shortR * 0.55);
        o.stroke();
        return off;
    });

    let leaves = [];
    let raf = null;
    let onResize = null;
    let lastFrame = 0;

    // 30 fps cap — drift is so slow that 30 vs 60 fps is invisible.
    const FRAME_INTERVAL_MS = 1000 / 30;
    let nextTick = 0;

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = window.innerWidth * dpr;
        canvas.height = window.innerHeight * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function rand(min, max) { return min + Math.random() * (max - min); }

    function spawnLeaf(seedAnywhere) {
        // Scale is the visual size relative to the sprite. 0.55..1.0
        // gives a useful size range without spawning a second sprite.
        const scale = rand(0.55, 1.0);
        return {
            x: rand(-30, window.innerWidth + 30),
            y: seedAnywhere ? rand(0, window.innerHeight) : rand(-120, -20),
            scale,
            rot: rand(0, Math.PI * 2),
            rotSpeed: rand(-0.30, 0.30),
            vy: rand(22, 44),
            swayAmp: rand(12, 32),
            swaySpeed: rand(0.0004, 0.0011),
            phase: rand(0, Math.PI * 2),
            spriteIdx: Math.floor(Math.random() * sprites.length),
            alpha: rand(0.75, 1.0),
        };
    }

    function ensureCount() {
        // Tighter than before (max 16) — sprites are easier on the RPi
        // but lower count is still free perf.
        const target = Math.min(16, Math.max(8, Math.floor(window.innerWidth * window.innerHeight / 110000)));
        while (leaves.length < target) leaves.push(spawnLeaf(false));
    }

    function drawLeaf(l) {
        const sprite = sprites[l.spriteIdx];
        const drawSize = SPRITE_SIZE * l.scale;
        ctx.save();
        ctx.globalAlpha = l.alpha;
        ctx.translate(l.x, l.y);
        ctx.rotate(l.rot);
        // drawImage(image, dx, dy, dw, dh) — centre the sprite on the
        // current origin so rotation/translation feel natural.
        ctx.drawImage(sprite, -drawSize / 2, -drawSize / 2, drawSize, drawSize);
        ctx.restore();
    }

    function tick(now) {
        if (raf === null) return;
        if (now < nextTick) {
            raf = requestAnimationFrame(tick);
            return;
        }
        nextTick = now + FRAME_INTERVAL_MS;
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
        leaves = leaves.filter(l => l.y - SPRITE_SIZE * l.scale * 0.6 < window.innerHeight + 30);
        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            leaves = [];
            for (let i = 0; i < 12; i++) leaves.push(spawnLeaf(true));
            requestAnimationFrame(() => { canvas.style.opacity = "1"; });
            lastFrame = performance.now();
            nextTick = 0;
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
