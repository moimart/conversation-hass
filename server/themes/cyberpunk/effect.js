/* Cyberpunk effect — a slow horizontal sweep line, occasional
   chromatic-glitch bars, and an idle hum of static specks. All drawn
   onto a canvas the effect mounts itself (no published HTML hook
   required) and removes again on stop().

   Tuned to be visually present but never seizure-inducing: most
   frames change very little; the obvious flicker is gated to a few
   short bursts every ~5 seconds. */

export default function setup({ root }) {
    const canvas = root.ownerDocument.createElement("canvas");
    canvas.id = "cyberpunk-fx";
    Object.assign(canvas.style, {
        position: "absolute",
        inset: "0",
        width: "100%",
        height: "100%",
        zIndex: "0",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 0.4s ease",
    });
    root.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    let raf = null;
    let onResize = null;
    let glitches = [];   // [{y, h, until, hue}]
    let sweepY = 0;
    let lastGlitch = 0;
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

    function spawnGlitch(now) {
        // Pick a random Y near the middle band (where the orb is) so
        // the bar feels like it's cutting through HAL's eye.
        const y = rand(areaH() * 0.15, areaH() * 0.85);
        const h = rand(3, 22);
        const until = now + rand(80, 260);
        // Mostly yellow, sometimes cyan or magenta for chromatic shift.
        const palette = ["#fcee0a", "#00f0ff", "#ff2a6d"];
        const hue = palette[Math.floor(Math.random() * palette.length)];
        glitches.push({ y, h, until, hue, shift: rand(-12, 12) });
    }

    function draw(now) {
        if (raf === null) return;
        const dt = now - (lastFrame || now);
        lastFrame = now;

        // Clear (we redraw everything every frame; cheap because the
        // canvas mostly contains thin lines and a few bars).
        ctx.clearRect(0, 0, areaW(), areaH());

        // 1) The slow horizontal sweep — a thin yellow line drifting
        //    downward, with a long soft tail above it.
        sweepY = (sweepY + dt * 0.06) % (areaH() + 120);
        const gradient = ctx.createLinearGradient(0, sweepY - 80, 0, sweepY + 4);
        gradient.addColorStop(0, "rgba(252, 238, 10, 0)");
        gradient.addColorStop(0.85, "rgba(252, 238, 10, 0.08)");
        gradient.addColorStop(1, "rgba(252, 238, 10, 0.55)");
        ctx.fillStyle = gradient;
        ctx.fillRect(0, sweepY - 80, areaW(), 84);

        // 2) Idle static — a sparse scatter of single-pixel specks,
        //    redrawn every frame so they twinkle.
        ctx.fillStyle = "rgba(0, 240, 255, 0.18)";
        const speckCount = Math.min(120, Math.floor(areaW() / 12));
        for (let i = 0; i < speckCount; i++) {
            ctx.fillRect(
                Math.random() * areaW() | 0,
                Math.random() * areaH() | 0,
                1, 1,
            );
        }

        // 3) Glitch bars: chromatic-shift slashes that flash briefly.
        //    Schedule a new one every ~5s on average.
        if (now - lastGlitch > rand(2500, 7500)) {
            spawnGlitch(now);
            // 30% chance of a quick stacked second bar for combo effect.
            if (Math.random() < 0.3) spawnGlitch(now);
            lastGlitch = now;
        }
        glitches = glitches.filter(g => g.until > now);
        for (const g of glitches) {
            ctx.fillStyle = g.hue + "cc";  // alpha ~0.8
            ctx.fillRect(g.shift, g.y, areaW(), g.h);
            // A faint cyan ghost just below for chromatic-aberration vibe
            ctx.fillStyle = "rgba(0, 240, 255, 0.35)";
            ctx.fillRect(-g.shift, g.y + 1, areaW(), g.h);
        }

        raf = requestAnimationFrame(draw);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
            requestAnimationFrame(() => { canvas.style.opacity = "0.45"; });
            lastFrame = performance.now();
            raf = requestAnimationFrame(draw);
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
            glitches = [];
        },
    };
}
