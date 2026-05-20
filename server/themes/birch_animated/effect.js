/* Birch — Animated ambient effect.
   ---------------------------------
   Soft warm motes drifting *diagonally* across the page, like afternoon
   sunlight catching dust through a south-facing window. Visually
   distinct from the sunset effect (which floats straight up like
   golden-hour bokeh): motes here drift on a slow lateral path with a
   subtle vertical bob, and the palette is warm copper/amber rather
   than coral/peach. */

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

    // Warm birch palette — copper, amber, bronze, cream. Slightly
    // higher base alphas than sunset so the wash is noticeable on the
    // lighter beige background (mix-blend isn't available on canvas
    // particles the same way, so we lean on alpha instead).
    const PALETTE = [
        "rgba(217, 122,  53, 0.65)",   // amber
        "rgba(201, 110,  31, 0.62)",   // copper
        "rgba(232, 165, 100, 0.60)",   // soft bronze
        "rgba(255, 232, 184, 0.55)",   // cream
    ];

    let particles = [];
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

    function spawnParticle(seedAnywhere) {
        // Default drift direction is left-to-right with a slight downward
        // tilt; sign of vx randomly flips per particle so the group
        // doesn't look like a one-way conveyor belt.
        const dirSign = Math.random() < 0.55 ? 1 : -1;
        return {
            x: seedAnywhere
                ? rand(0, window.innerWidth)
                : (dirSign > 0 ? rand(-200, -20) : rand(window.innerWidth + 20, window.innerWidth + 200)),
            y: rand(0, window.innerHeight),
            r: rand(32, 86),                       // soft bokeh radius
            vx: dirSign * rand(10, 26),            // px/sec
            vy: rand(-3, 4),                       // gentle vertical wander
            bobAmp: rand(6, 22),
            bobSpeed: rand(0.0004, 0.0011),
            phase: rand(0, Math.PI * 2),
            color: PALETTE[Math.floor(Math.random() * PALETTE.length)],
            alpha: rand(0.55, 0.95),
        };
    }

    function ensureCount() {
        // Roughly the same density as the sunset effect so they feel
        // equally lived-in.
        const target = Math.min(30, Math.max(14, Math.floor(window.innerWidth * window.innerHeight / 65000)));
        while (particles.length < target) particles.push(spawnParticle(false));
    }

    function drawParticle(p) {
        const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
        grad.addColorStop(0, p.color);
        grad.addColorStop(0.55, p.color.replace(/0\.\d+\)/, "0.20)"));
        grad.addColorStop(1, "rgba(255, 220, 170, 0)");
        ctx.globalAlpha = p.alpha;
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
    }

    function tick(now) {
        if (raf === null) return;
        const dt = (now - (lastFrame || now)) / 1000;
        lastFrame = now;
        ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
        ensureCount();
        for (const p of particles) {
            p.x += p.vx * dt;
            p.y += p.vy * dt + Math.sin(now * p.bobSpeed + p.phase) * p.bobAmp * dt * 0.4;
            drawParticle(p);
        }
        // Recycle particles that have drifted fully off either side or
        // top/bottom.
        particles = particles.filter(p =>
            p.x + p.r > -20 && p.x - p.r < window.innerWidth + 20 &&
            p.y + p.r > -200 && p.y - p.r < window.innerHeight + 200
        );
        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            particles = [];
            // Seed across the viewport so the page is populated from
            // the first frame.
            for (let i = 0; i < 18; i++) particles.push(spawnParticle(true));
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
            particles = [];
        },
    };
}
