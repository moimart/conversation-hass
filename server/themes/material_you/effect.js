/* Material You effect — RPi-friendly lava-lamp drift.
   ---------------------------------------------------
   Five soft Google-colour blobs gently drift across the screen on
   their own slow paths. Implemented as plain <div> elements with a
   radial-gradient background (the gradient does the soft falloff, so
   no expensive CSS filter:blur is needed) and animated via the Web
   Animations API. Keyframes are compiled once and the transform
   interpolation runs on the compositor thread — there's no JS per
   frame and no per-frame canvas redraw, which is what makes the
   canvas version painful on a Raspberry Pi. */

export default function setup({ root }) {
    const doc = root.ownerDocument;

    const container = doc.createElement("div");
    container.id = "matyou-fx";
    Object.assign(container.style, {
        position: "absolute",
        inset: "0",
        zIndex: "0",
        pointerEvents: "none",
        opacity: "0",
        transition: "opacity 1.2s ease",
        overflow: "hidden",
        contain: "layout paint",
    });
    root.appendChild(container);

    // Google brand palette plus one soft secondary blue. Each blob
    // gets its own slow loop with an irregular 4-step path and its
    // own period so the wash never lines up periodically across the
    // group — the eye never sees the loop.
    const RECIPES = [
        // [r, g, b], coreAlpha, size(vmin), durationSec, path([x%, y%] of viewport, last == first to close the loop)
        { rgb: "66, 133, 244",  a: 0.75, size: 64, dur: 71, path: [[10, 20], [75,  8], [55, 70], [-8, 62], [10, 20]] },  // Blue
        { rgb: "234,  67,  53", a: 0.62, size: 58, dur: 83, path: [[78, 22], [22, 78], [70, 92], [96, 38], [78, 22]] },  // Red
        { rgb: "251, 188,   4", a: 0.62, size: 60, dur: 67, path: [[42, 86], [88, 50], [12, 30], [52,  4], [42, 86]] },  // Yellow
        { rgb: " 52, 168,  83", a: 0.62, size: 62, dur: 79, path: [[25, 52], [62, 86], [82, 18], [ 8, 28], [25, 52]] },  // Green
        { rgb: "132, 168, 235", a: 0.50, size: 52, dur: 91, path: [[58, 42], [12, 60], [42, 92], [86, 70], [58, 42]] },  // Soft secondary blue
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
            // The gradient drops to fully transparent at 68% of the
            // radius — that's what gives the soft lava edge without
            // needing a CSS blur on top.
            background:
                "radial-gradient(circle at 50% 50%, " +
                `rgba(${r.rgb}, ${r.a}) 0%, ` +
                `rgba(${r.rgb}, ${(r.a * 0.32).toFixed(3)}) 38%, ` +
                `rgba(${r.rgb}, 0) 68%)`,
            // Initial position via translate so the WAAPI keyframes
            // are pure transform updates (compositor-only).
            transform: "translate3d(0, 0, 0)",
            willChange: "transform",
        });
        container.appendChild(blob);

        // Build absolute-positioned keyframes. The blob's top/left
        // are 0; we move it with a translate by (x% of vw, y% of vh)
        // minus half the blob size so its CENTRE lands on the path
        // point.
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
            // Defer the fade-in by a frame so the animations have
            // already placed the blobs at frame 0 — avoids a flash
            // of all blobs at (0,0) before the WAAPI ticks.
            requestAnimationFrame(() => {
                container.style.opacity = "0.95";
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
