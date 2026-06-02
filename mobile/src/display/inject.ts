// Mount the reused kiosk display into the shell.
//
// The display markup lives in www/display.html (copied from rpi/web/index.html).
// We inject its <body> elements (minus its <script> tags) into #display-root,
// then load pairing_overlay.js + app.js as classic scripts. app.js is an IIFE
// that connects to HAL_CONFIG.wsUrl on load and lazy-imports photo_frame.js /
// calendar.js / state_videos.js itself (relative/absolute paths resolve to
// www/ where sync-web copied them). HAL_CONFIG MUST already be set on window.

function loadScript(src: string, module = false): Promise<void> {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    if (module) s.type = "module";
    s.src = src;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(s);
  });
}

let mounted = false;

export async function mountDisplay(): Promise<void> {
  if (mounted) return;
  mounted = true;

  const html = await (await fetch("display.html", { cache: "no-store" })).text();
  const doc = new DOMParser().parseFromString(html, "text/html");
  const root = document.getElementById("display-root");
  if (!root) throw new Error("display-root missing");

  // Move every body child except <script>/<link> into the shell.
  for (const node of Array.from(doc.body.childNodes)) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = (node as Element).tagName;
      if (tag === "SCRIPT" || tag === "LINK") continue;
    }
    root.appendChild(document.importNode(node, true));
  }

  // HLS.js is best-effort (only used for .m3u8 play_video); don't block on it.
  loadScript("https://cdn.jsdelivr.net/npm/hls.js@1.5/dist/hls.min.js").catch(
    () => console.warn("[hal] hls.js unavailable — m3u8 video disabled"),
  );

  // NOTE: we deliberately do NOT load pairing_overlay.js here. The pairing code
  // is meant for the kiosk DISPLAY; the phone is where you ENTER it. If the
  // phone also rendered the code overlay it would cover its own input. app.js's
  // show_pairing_code case is a no-op when window.HALPairingOverlay is absent.

  // app.js (IIFE) connects + drives the display using window.HAL_CONFIG.
  await loadScript("app.js");

  // app.js sizes the orientation wrapper from the viewport at load. In the
  // WebView the final size isn't settled until after the splash hides, so nudge
  // a relayout (app.js re-applies orientation on resize) once things settle.
  for (const ms of [250, 700, 1500]) {
    setTimeout(() => window.dispatchEvent(new Event("resize")), ms);
  }
}
