/* Matrix digital-rain effect. ES module — the kiosk imports the
   default export and calls setup({root}) when the theme is activated.
   The returned controller's start()/stop() are called as the user
   switches into and out of the theme. */

export default function setup({ root }) {
    const MATRIX_CHARS = (
        "アイウエオカキクケコサシスセソタチツテトナニヌネノ" +
        "ハヒフヘホマミムメモヤユヨラリルレロワヲン" +
        "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    ).split("");

    // The kiosk page already has a <canvas id="matrix-rain"> baked into
    // the HTML and styled in the base CSS — we just animate it.
    const canvas = root.ownerDocument.getElementById("matrix-rain");
    if (!canvas) {
        return { start() {}, stop() {} };
    }
    const ctx = canvas.getContext("2d");

    let drops = null;
    let raf = null;
    let onResize = null;
    const fontSize = 18;

    function areaW() { return (canvas.parentElement && canvas.parentElement.clientWidth) || window.innerWidth; }
    function areaH() { return (canvas.parentElement && canvas.parentElement.clientHeight) || window.innerHeight; }

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = areaW() * dpr;
        canvas.height = areaH() * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const cols = Math.floor(areaW() / fontSize);
        drops = new Array(cols).fill(0).map(() => Math.random() * -50);
    }

    function tick() {
        if (raf === null) return;
        ctx.fillStyle = "rgba(0, 8, 6, 0.08)";
        ctx.fillRect(0, 0, areaW(), areaH());
        ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
        ctx.textBaseline = "top";
        for (let i = 0; i < drops.length; i++) {
            const ch = MATRIX_CHARS[Math.floor(Math.random() * MATRIX_CHARS.length)];
            const y = drops[i] * fontSize;
            ctx.fillStyle = "#d8ffe0";
            ctx.fillText(ch, i * fontSize, y);
            ctx.fillStyle = "#00ff41";
            ctx.fillText(ch, i * fontSize, y - fontSize);
            if (y > areaH() && Math.random() > 0.975) {
                drops[i] = 0;
            }
            drops[i] += 1;
        }
        raf = requestAnimationFrame(tick);
    }

    return {
        start() {
            if (raf !== null) return;
            resize();
            onResize = () => resize();
            window.addEventListener("resize", onResize);
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
        },
    };
}
