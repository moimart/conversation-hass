/* Sunset effect — soft warm bokeh particles drifting slowly upward,
   like dust motes or pollen catching the last hour of light. Very
   subtle: each particle is a low-opacity blurred radial gradient,
   and only a handful (15–25) are on screen at once. */

export default function setup({ root }) {
    const canvas = root.ownerDocument.createElement("canvas");
    canvas.id = "sunset-animated-fx";
    Object.assign(canvas.style, {
        position: "absolute",
        inset: "0",
        width: "100%",
        height: "100%",
        zIndex: "0",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 1.2s ease",
    });
    root.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    // Warm palette: coral, peach, soft gold.
    const PALETTE = [
        "rgba(255, 180, 130, 0.55)",   // coral
        "rgba(255, 215, 170, 0.55)",   // peach
        "rgba(255, 235, 200, 0.55)",   // soft gold
        "rgba(255, 165, 120, 0.50)",   // deeper coral
    ];

    let particles = [];
    let raf = null;
    let onResize = null;
    let lastFrame = 0;

    function areaW() { return root.clientWidth || window.innerWidth; }
    function areaH() { return root.clientHeight || window.innerHeight; }

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = areaW() * dpr;
        canvas.height = areaH() * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function rand(min, max) { return min + Math.random() * (max - min); }

    function spawnParticle(startBelow) {
        return {
            x: rand(0, areaW()),
            y: startBelow ? areaH() + rand(10, 200) : rand(0, areaH()),
            r: rand(28, 76),                          // bokeh radius (big & blurry)
            vy: rand(-8, -22),                        // upward drift, px/sec
            swayAmp: rand(8, 30),                     // horizontal sway amplitude
            swaySpeed: rand(0.0003, 0.0009),          // sway angular freq
            phase: rand(0, Math.PI * 2),
            color: PALETTE[Math.floor(Math.random() * PALETTE.length)],
            alpha: rand(0.35, 0.85),                  // overall opacity multiplier
        };
    }

    function ensureCount(now) {
        const target = Math.min(28, Math.max(12, Math.floor(areaW() * areaH() / 70000)));
        while (particles.length < target) particles.push(spawnParticle(true));
    }

    function drawParticle(p) {
        const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
        grad.addColorStop(0, p.color);
        grad.addColorStop(0.6, p.color.replace(/0\.\d+\)/, "0.18)"));
        grad.addColorStop(1, "rgba(255, 200, 150, 0)");
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
        ctx.clearRect(0, 0, areaW(), areaH());
        ensureCount(now);
        for (const p of particles) {
            // Drift upward with a gentle horizontal sway.
            p.y += p.vy * dt;
            p.x += Math.sin(now * p.swaySpeed + p.phase) * p.swayAmp * dt * 0.3;
            drawParticle(p);
        }
        // Recycle particles that have drifted off the top.
        particles = particles.filter(p => p.y + p.r > -10);
        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            particles = [];
            // Seed with a few mid-screen so the effect starts populated
            // instead of having to wait for them to drift up from below.
            for (let i = 0; i < 10; i++) particles.push(spawnParticle(false));
            requestAnimationFrame(() => { canvas.style.opacity = "0.7"; });
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
