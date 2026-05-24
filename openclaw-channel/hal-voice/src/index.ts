/**
 * HAL Voice Kiosk — OpenClaw channel plugin entry point.
 *
 * Receives STT-transcribed text from a HAL kiosk via HTTP webhook,
 * routes it through the OpenClaw agent (with full tool access including
 * mcporter/MCP), and pushes the agent's text response back to the kiosk
 * for TTS playback via HAL's /api/speak endpoint.
 */

import { defineChannelPluginEntry } from "openclaw/plugin-sdk/channel-entry-contract";
import { halVoicePlugin } from "./channel.js";

export default defineChannelPluginEntry({
  id: "hal-voice",
  name: "HAL Voice Kiosk",
  description: "Voice assistant kiosk channel — STT in, TTS out, orb display for media.",
  plugin: halVoicePlugin,

  registerFull(api: any) {
    // HTTP webhook endpoint: HAL POSTs { text, sender? } here when
    // it receives STT output. The gateway routes it to the agent.
    api.registerHttpRoute({
      path: "/hal-voice/webhook",
      auth: "plugin",
      handler: async (req: any, res: any) => {
        try {
          const body = await readJsonBody(req);
          const text = body?.text?.trim();
          if (!text) {
            res.statusCode = 400;
            res.end(JSON.stringify({ error: "missing text" }));
            return true;
          }

          const sender = body?.sender || "kiosk-user";
          const halUrl = body?.hal_server_url || "";

          // Route to the inbound handler (fires agent processing)
          await handleHalInbound(api, { text, sender, halUrl });

          res.statusCode = 200;
          res.end(JSON.stringify({ status: "ok", text_length: text.length }));
        } catch (err: any) {
          res.statusCode = 500;
          res.end(JSON.stringify({ error: err?.message || "internal error" }));
        }
        return true;
      },
    });
  },
});

async function readJsonBody(req: any): Promise<any> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        resolve(null);
      }
    });
    req.on("error", reject);
  });
}

async function handleHalInbound(api: any, params: {
  text: string;
  sender: string;
  halUrl: string;
}) {
  // Build inbound turn context for the channel engine.
  // The gateway routes this to the agent with full tool access.
  const { buildChannelTurnContext } = await import(
    "openclaw/plugin-sdk/channel-inbound"
  );

  const turnContext = buildChannelTurnContext({
    channel: "hal-voice",
    accountId: "default",
    conversation: {
      kind: "direct",
      id: `hal:voice:${params.sender}`,
    },
    message: {
      text: params.text,
      senderId: params.sender,
      senderDisplayName: "Kiosk User",
      timestamp: Date.now(),
    },
  });

  await api.emitChannelInbound(turnContext);
}
