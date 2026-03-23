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

    // --- State ---
    let ws = null;
    let reconnectTimer = null;
    let currentPartialEl = null;
    const MAX_TRANSCRIPT_LINES = 50;

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
            case "pong":
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

        // Fade out response after a delay
        clearTimeout(handleResponse._fadeTimer);
        handleResponse._fadeTimer = setTimeout(() => {
            responseContainer.classList.remove("visible");
        }, 12000);
    }

    // --- State Management ---
    function setState(state) {
        document.body.className = "";

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
    }

    // --- Keepalive ---
    setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
        }
    }, 30000);

    // --- Init ---
    setState("idle");
    connect();
})();
