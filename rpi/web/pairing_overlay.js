/* Pairing-code overlay — shared by the kiosk and the mobile companion app.
 *
 * When the user asks HAL to pair a phone, the server mints a short-lived code
 * and broadcasts {"type":"show_pairing_code","code","expires_in"} to every UI
 * client (and the kiosk via the RPi). app.js dispatches that to
 * window.HALPairingOverlay.show(); the matching hide_pairing_code (sent on
 * redeem or expiry) calls .hide().
 *
 * Self-contained: builds its own DOM + styles at a very high z-index so it sits
 * above the orb, photo frame, and calendar. No edits to style.css. Plain script
 * (sets a window global) so both index.html and the mobile shell can load it
 * with a single <script> tag before app.js. */
(function () {
    "use strict";

    var root = null;
    var codeEl = null;
    var countEl = null;
    var timer = null;

    function ensureDom(doc) {
        if (root) return;
        var style = doc.createElement("style");
        style.textContent =
            ".hal-pair-overlay{position:fixed;inset:0;z-index:2147482000;display:none;" +
            "align-items:center;justify-content:center;background:rgba(0,0,0,0.82);" +
            "backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);" +
            "font-family:'Berlin Type','JetBrains Mono',monospace;color:#fff;text-align:center;}" +
            ".hal-pair-overlay.visible{display:flex;}" +
            ".hal-pair-card{padding:6vmin 8vmin;border:1px solid rgba(255,255,255,0.18);" +
            "border-radius:18px;background:rgba(20,22,28,0.55);box-shadow:0 12px 60px rgba(0,0,0,0.6);}" +
            ".hal-pair-title{font-size:3.2vmin;letter-spacing:0.18em;text-transform:uppercase;" +
            "opacity:0.7;margin-bottom:3vmin;}" +
            ".hal-pair-code{font-size:14vmin;font-weight:600;letter-spacing:0.16em;line-height:1;" +
            "text-shadow:0 2px 14px rgba(0,0,0,0.6);}" +
            ".hal-pair-sub{margin-top:3vmin;font-size:2.4vmin;opacity:0.55;letter-spacing:0.08em;}";
        doc.head.appendChild(style);

        root = doc.createElement("div");
        root.className = "hal-pair-overlay";
        root.setAttribute("aria-hidden", "true");
        var card = doc.createElement("div");
        card.className = "hal-pair-card";
        var title = doc.createElement("div");
        title.className = "hal-pair-title";
        title.textContent = "Pair your device";
        codeEl = doc.createElement("div");
        codeEl.className = "hal-pair-code";
        codeEl.textContent = "------";
        countEl = doc.createElement("div");
        countEl.className = "hal-pair-sub";
        countEl.textContent = "";
        card.appendChild(title);
        card.appendChild(codeEl);
        card.appendChild(countEl);
        root.appendChild(card);
        // Mount INSIDE #orientation-wrapper so the code follows the kiosk's
        // portrait/landscape orientation — the wrapper's rotate() transform also
        // rotates this (and makes its position:fixed resolve against the
        // wrapper box). Fall back to body where there's no wrapper.
        (doc.getElementById("orientation-wrapper") || doc.body).appendChild(root);
    }

    function clearTimer() {
        if (timer) { clearInterval(timer); timer = null; }
    }

    function show(code, expiresIn) {
        var doc = document;
        ensureDom(doc);
        clearTimer();
        codeEl.textContent = String(code || "------");
        var remaining = parseInt(expiresIn, 10);
        if (!isFinite(remaining) || remaining <= 0) remaining = 0;
        function render() {
            countEl.textContent = remaining > 0
                ? "Enter this code in the app · " + remaining + "s"
                : "Enter this code in the app";
        }
        render();
        root.classList.add("visible");
        root.setAttribute("aria-hidden", "false");
        if (remaining > 0) {
            timer = setInterval(function () {
                remaining -= 1;
                if (remaining <= 0) { clearTimer(); }
                render();
            }, 1000);
        }
    }

    function hide() {
        clearTimer();
        if (root) {
            root.classList.remove("visible");
            root.setAttribute("aria-hidden", "true");
        }
    }

    window.HALPairingOverlay = { show: show, hide: hide };
})();
