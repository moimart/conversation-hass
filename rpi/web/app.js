/**
 * HAL 9000 — Web UI Client
 *
 * Connects via WebSocket to the local RPi audio streamer service
 * to receive live transcription and AI responses.
 */

(() => {
    "use strict";

    // --- DOM Elements ---
    const transcript = document.getElementById("transcript");
    const responseContainer = document.getElementById("response-container");
    const responseText = document.getElementById("response-text");
    const connectionEl = document.getElementById("connection");
    const statusText = document.querySelector(".status-text");
    const themeSelect = document.getElementById("theme-select");

    // --- State ---
    let ws = null;
    let reconnectTimer = null;
    let currentPartialEl = null;
    const MAX_TRANSCRIPT_LINES = 50;

    // --- Theme ---
    const THEME_KEY = "hal-theme";
    const THEMES = ["dark", "birch", "odyssey", "japandi"];

    function applyTheme(name) {
        if (!THEMES.includes(name)) name = "dark";
        // Remove any existing theme-* class
        document.body.classList.forEach(c => {
            if (c.startsWith("theme-")) document.body.classList.remove(c);
        });
        if (name !== "dark") {
            document.body.classList.add(`theme-${name}`);
        }
        if (themeSelect) themeSelect.value = name;
    }

    const savedTheme = localStorage.getItem(THEME_KEY) || "dark";
    applyTheme(savedTheme);

    if (themeSelect) {
        themeSelect.addEventListener("change", (e) => {
            const choice = e.target.value;
            localStorage.setItem(THEME_KEY, choice);
            applyTheme(choice);
        });
    }

    // --- WebSocket ---
    function connect() {
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${protocol}//${location.host}/ws`;

        ws = new WebSocket(url);

        ws.onopen = () => {
            console.log("Connected to HAL");
            connectionEl.className = "connection-indicator connected";
            connectionEl.querySelector("span").textContent = "CONNECTED";
            clearTimeout(reconnectTimer);
        };

        ws.onclose = () => {
            console.log("Disconnected");
            connectionEl.className = "connection-indicator";
            connectionEl.querySelector("span").textContent = "DISCONNECTED";
            setState("idle");
            scheduleReconnect();
        };

        ws.onerror = (err) => {
            console.error("WebSocket error:", err);
            ws.close();
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                console.error("Failed to parse message:", e);
            }
        };
    }

    function scheduleReconnect() {
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 3000);
    }

    // --- Message Handling ---
    function handleMessage(msg) {
        switch (msg.type) {
            case "transcription":
                handleTranscription(msg);
                break;
            case "wake":
                handleWake();
                break;
            case "response":
                handleResponse(msg);
                break;
            case "state":
                setState(msg.state);
                break;
            case "mute_sync":
                // Hardware mute button pressed — sync UI
                muted = !!msg.muted;
                muteBtn.classList.toggle("muted", muted);
                muteIconOn.style.display = muted ? "none" : "";
                muteIconOff.style.display = muted ? "" : "none";
                break;
            case "volume_sync":
                // Hardware volume button pressed — sync UI
                volume = Math.max(0, Math.min(1, msg.level));
                volFill.style.width = (volume * 100) + "%";
                break;
            case "pong":
                break;
            case "set_theme":
                if (msg.name && THEMES.includes(msg.name)) {
                    localStorage.setItem(THEME_KEY, msg.name);
                    applyTheme(msg.name);
                }
                break;
            case "show_camera":
                showCamera(msg);
                break;
            case "stream_start":
                startStream(msg);
                break;
            case "stream_stop":
                stopStream();
                break;
            case "webrtc_signal":
                handleSignal(msg);
                break;
            default:
                console.log("Unknown message:", msg);
        }
    }

    function handleTranscription(msg) {
        const { text, is_partial, speaker } = msg;

        if (!text || !text.trim()) return;

        // Remove placeholder
        const placeholder = transcript.querySelector(".transcript-placeholder");
        if (placeholder) placeholder.remove();

        if (is_partial) {
            // Update or create partial line
            if (!currentPartialEl) {
                currentPartialEl = document.createElement("div");
                currentPartialEl.className = "transcript-line partial latest";
                transcript.appendChild(currentPartialEl);
            }
            currentPartialEl.textContent = text;
            setState("listening");
        } else {
            // Finalize: replace partial with final
            if (currentPartialEl) {
                currentPartialEl.remove();
                currentPartialEl = null;
            }

            // Remove 'latest' from previous lines
            transcript.querySelectorAll(".latest").forEach(el => el.classList.remove("latest"));

            const line = document.createElement("div");
            line.className = "transcript-line latest";
            if (speaker === "ai") {
                line.style.color = "var(--accent)";
                line.style.opacity = "0.5";
            }
            line.textContent = text;
            transcript.appendChild(line);

            // Prune old lines
            while (transcript.children.length > MAX_TRANSCRIPT_LINES) {
                transcript.removeChild(transcript.firstChild);
            }
        }

        // Auto-scroll
        transcript.scrollTop = transcript.scrollHeight;
    }

    function handleWake() {
        // Flash the eye and show wake indicator
        const eye = document.querySelector(".eye-container");
        eye.classList.add("wake-flash");
        setState("listening");

        // Remove flash after animation completes
        setTimeout(() => eye.classList.remove("wake-flash"), 800);
    }

    function handleResponse(msg) {
        const { text } = msg;
        if (!text) return;

        responseText.textContent = text;
        responseContainer.classList.add("visible");
        setState("speaking");
    }

    // --- Camera-in-orb display ---
    let cameraTimer = null;
    function showCamera(msg) {
        const container = document.querySelector(".eye-container");
        const inner = document.querySelector(".eye-bezel-inner");
        if (!container || !inner || !msg.image) return;
        // Snapshot replaces any active live stream.
        stopStream();
        const mime = msg.mime || "image/jpeg";
        const dur = Math.max(5, Math.min(900, Number(msg.duration_s) || 150));
        if (cameraTimer) clearTimeout(cameraTimer);
        inner.style.backgroundImage = `url("data:${mime};base64,${msg.image}")`;
        container.classList.add("camera-active");
        container.classList.remove("stream-active");
        cameraTimer = setTimeout(clearCamera, dur * 1000);
    }
    function clearCamera() {
        const container = document.querySelector(".eye-container");
        const inner = document.querySelector(".eye-bezel-inner");
        if (inner) inner.style.backgroundImage = "";
        if (container) container.classList.remove("camera-active");
        cameraTimer = null;
    }

    // --- Live WebRTC stream-in-orb ---
    // The kiosk owns the RTCPeerConnection. The server proxies signaling
    // to Home Assistant's camera/webrtc/offer subscription.
    let streamPC = null;
    let streamSessionId = null;
    function startStream(msg) {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (!container || !video) return;
        // A new stream replaces any existing one (snapshot or stream).
        clearCamera();
        stopStream();
        streamSessionId = msg.session_id || "";
        const pc = new RTCPeerConnection();
        streamPC = pc;
        pc.addTransceiver("video", { direction: "recvonly" });
        pc.ontrack = (ev) => {
            if (ev.streams && ev.streams[0]) video.srcObject = ev.streams[0];
        };
        pc.onicecandidate = (ev) => {
            if (!ev.candidate || !ws || ws.readyState !== WebSocket.OPEN) return;
            ws.send(JSON.stringify({
                type: "webrtc_signal",
                session_id: streamSessionId,
                kind: "candidate",
                candidate: ev.candidate.candidate,
                sdpMid: ev.candidate.sdpMid,
                sdpMLineIndex: ev.candidate.sdpMLineIndex,
            }));
        };
        pc.onconnectionstatechange = () => {
            if (pc.connectionState === "failed" || pc.connectionState === "closed") {
                stopStream();
            }
        };
        container.classList.add("camera-active", "stream-active");
        pc.createOffer()
            .then((offer) => pc.setLocalDescription(offer).then(() => offer))
            .then((offer) => {
                if (!ws || ws.readyState !== WebSocket.OPEN) return;
                ws.send(JSON.stringify({
                    type: "webrtc_signal",
                    session_id: streamSessionId,
                    kind: "offer",
                    sdp: offer.sdp,
                }));
            })
            .catch((e) => {
                console.warn("WebRTC offer failed:", e);
                stopStream();
            });
    }
    function handleSignal(msg) {
        if (!streamPC || !streamSessionId || msg.session_id !== streamSessionId) return;
        if (msg.kind === "answer" && msg.sdp) {
            streamPC.setRemoteDescription({ type: "answer", sdp: msg.sdp }).catch((e) => {
                console.warn("setRemoteDescription failed:", e);
            });
        } else if (msg.kind === "candidate" && msg.candidate) {
            streamPC.addIceCandidate({
                candidate: msg.candidate,
                sdpMid: msg.sdpMid || null,
                sdpMLineIndex: msg.sdpMLineIndex == null ? null : msg.sdpMLineIndex,
            }).catch((e) => {
                console.debug("addIceCandidate ignored:", e);
            });
        }
    }
    function stopStream() {
        const container = document.querySelector(".eye-container");
        const video = document.getElementById("eye-stream");
        if (streamPC) {
            try { streamPC.close(); } catch (e) { /* ignore */ }
            streamPC = null;
        }
        streamSessionId = null;
        if (video) {
            try { video.srcObject = null; } catch (e) { /* ignore */ }
        }
        if (container) {
            container.classList.remove("stream-active");
            // Drop camera-active too if no snapshot is on screen
            if (!cameraTimer) container.classList.remove("camera-active");
        }
    }

    // --- State Management ---
    function setState(state) {
        // Remove only state-* classes — preserve theme-* and any others
        Array.from(document.body.classList).forEach(c => {
            if (c.startsWith("state-")) document.body.classList.remove(c);
        });

        const labels = {
            idle: "IDLE",
            listening: "LISTENING",
            processing: "PROCESSING",
            speaking: "SPEAKING",
        };

        if (state && labels[state]) {
            document.body.classList.add(`state-${state}`);
            statusText.textContent = labels[state];
        }

        // Hide previous response when HAL starts thinking about a new question
        if (state === "processing" && responseContainer.classList.contains("visible")) {
            clearTimeout(setState._fadeTimer);
            responseContainer.classList.remove("visible");
        }

        // Cancel any pending fade (we want the response to linger)
        if (state === "speaking") {
            clearTimeout(setState._fadeTimer);
        }
    }

    // --- Mute Control ---
    let muted = false;
    const muteBtn = document.getElementById("mute-btn");
    const muteIconOn = document.getElementById("mute-icon-on");
    const muteIconOff = document.getElementById("mute-icon-off");

    function setMuted(m) {
        muted = m;
        muteBtn.classList.toggle("muted", muted);
        muteIconOn.style.display = muted ? "none" : "";
        muteIconOff.style.display = muted ? "" : "none";
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "mute", muted: muted }));
        }
    }

    muteBtn.addEventListener("click", () => setMuted(!muted));

    // --- Volume Control ---
    let volume = 0.7;
    const volFill = document.getElementById("vol-fill");
    const volTrack = document.getElementById("vol-track");
    const volDown = document.getElementById("vol-down");
    const volUp = document.getElementById("vol-up");

    function setVolume(v) {
        volume = Math.max(0, Math.min(1, v));
        volFill.style.width = (volume * 100) + "%";
        // Send volume to RPi
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "volume", level: volume }));
        }
    }

    volDown.addEventListener("click", () => setVolume(volume - 0.1));
    volUp.addEventListener("click", () => setVolume(volume + 0.1));

    // Touch/click drag on the bar
    function handleVolDrag(e) {
        const rect = volTrack.getBoundingClientRect();
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        setVolume(pct);
    }

    volTrack.addEventListener("mousedown", (e) => {
        handleVolDrag(e);
        const onMove = (ev) => handleVolDrag(ev);
        const onUp = () => { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    volTrack.addEventListener("touchstart", (e) => {
        handleVolDrag(e);
        const onMove = (ev) => { ev.preventDefault(); handleVolDrag(ev); };
        const onEnd = () => { document.removeEventListener("touchmove", onMove); document.removeEventListener("touchend", onEnd); };
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onEnd);
    });

    // --- Keepalive ---
    setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
        }
    }, 30000);

    // --- Snapshot publisher: rasterize #app to JPEG and POST to local proxy ---
    async function publishSnapshot() {
        if (typeof html2canvas !== "function") return;
        if (document.hidden) return;
        try {
            const bg = getComputedStyle(document.body).backgroundColor || "#0a0a0c";
            const canvas = await html2canvas(document.body, {
                backgroundColor: bg,
                logging: false,
                useCORS: true,
                scale: 1,
            });
            const blob = await new Promise(r => canvas.toBlob(r, "image/jpeg", 0.7));
            if (!blob) return;
            await fetch("/api/snapshot", {
                method: "POST",
                body: blob,
                headers: { "Content-Type": "image/jpeg" },
            });
        } catch (e) {
            console.debug("snapshot failed:", e);
        }
    }
    setTimeout(publishSnapshot, 5000);
    setInterval(publishSnapshot, 60000);

    // --- Clock + date ---
    const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
    const MONTHS = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ];
    function updateClock() {
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, "0");
        const mm = String(now.getMinutes()).padStart(2, "0");
        const time = document.getElementById("clock-time");
        const date = document.getElementById("clock-date");
        if (time) time.textContent = `${hh}:${mm}`;
        if (date) {
            date.textContent = `${DAYS[now.getDay()]} ${now.getDate()} ${MONTHS[now.getMonth()]}`;
        }
    }
    updateClock();
    // Align next tick to the top of the next minute, then run every 60s.
    const msToNextMinute = 60000 - (Date.now() % 60000);
    setTimeout(() => {
        updateClock();
        setInterval(updateClock, 60000);
    }, msToNextMinute);

    // --- Init ---
    setVolume(0.7);
    setState("idle");
    connect();
})();
