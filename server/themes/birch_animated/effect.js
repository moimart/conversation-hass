/* Birch — Animated ambient effect.
   ---------------------------------
   Four very soft warm-toned light pools drift slowly across the page,
   evoking sunlight filtering through birch leaves onto a wooden floor.
   The visual rules:
     - low opacity (the wash should be felt, not seen)
     - long loop durations (90–130 s) with prime-ish offsets so the
       blobs never re-line-up periodically
     - radial-gradient does the soft edge, no CSS filter:blur (RPi)
     - Web Animations API drives pure transform keyframes
       (compositor-only — no JS per frame, no canvas) */

export default function setup({ root }) {
    const doc = root.ownerDocument;

    const container = doc.createElement("div");
    container.id = "birch-animated-fx";
    Object.assign(container.style, {
        position: "fixed",
        inset: "0",
        zIndex: "0",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 1.4s ease",
        overflow: "hidden",
        contain: "layout paint",
    });
    root.appendChild(container);

    // Warm birch palette — copper, amber, bronze, cream. Each blob
    // gets its own slow looping path. Opacities are deliberately low
    // so the wash reads as ambient light, not a pattern.
    const RECIPES = [
        // [r, g, b], coreAlpha, size(vmin), durationSec, 4-step closed path
        { rgb: "217, 122,  53", a: 0.22, size: 78, dur: 113, path: [[12, 18], [72, 12], [58, 78], [-6, 62], [12, 18]] },  // Amber
        { rgb: "178,  90,  28", a: 0.18, size: 72, dur: 127, path: [[78, 24], [22, 70], [70, 92], [94, 38], [78, 24]] },  // Copper
        { rgb: "201, 110,  31", a: 0.18, size: 68, dur:  97, path: [[42, 86], [88, 50], [16, 30], [52,  4], [42, 86]] },  // Bronze
        { rgb: "255, 232, 184", a: 0.16, size: 60, dur: 139, path: [[28, 50], [62, 88], [82, 18], [ 8, 30], [28, 50]] },  // Cream
    ];

    const anims = [];

    for (const r of RECIPES) {
        const blob = doc.createElement("div");
        Object.assign(blob.style, {
            position: "absolute",
            left: "0",
            top: "0",
            width: r.size + "vmin",
            height: r.size + "vmin",
            borderRadius: "50%",
            background:
                "radial-gradient(circle at 50% 50%, " +
                `rgba(${r.rgb}, ${r.a}) 0%, ` +
                `rgba(${r.rgb}, ${(r.a * 0.32).toFixed(3)}) 42%, ` +
                `rgba(${r.rgb}, 0) 72%)`,
            transform: "translate3d(0, 0, 0)",
            willChange: "transform",
            // mix-blend lets the warm pool "tint" the beige background
            // without darkening it — the result feels like light, not
            // a colored shape.
            mixBlendMode: "screen",
        });
        container.appendChild(blob);

        const half = r.size / 2;
        const keyframes = r.path.map(([x, y]) => ({
            transform: `translate3d(calc(${x}vw - ${half}vmin), calc(${y}vh - ${half}vmin), 0)`,
        }));
        anims.push({ blob, keyframes, dur: r.dur, ctrl: null });
    }

    let started = false;

    return {
        start() {
            if (started) return;
            started = true;
            for (const a of anims) {
                a.ctrl = a.blob.animate(a.keyframes, {
                    duration: a.dur * 1000,
                    iterations: Infinity,
                    easing: "ease-in-out",
                });
            }
            requestAnimationFrame(() => {
                container.style.opacity = "1";
            });
        },
        stop() {
            for (const a of anims) {
                try { a.ctrl && a.ctrl.cancel(); } catch (_) { /* ignore */ }
                a.ctrl = null;
            }
            container.style.opacity = "0";
            started = false;
        },
    };
}
