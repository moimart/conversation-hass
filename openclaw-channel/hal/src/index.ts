import { defineChannelPluginEntry } from "openclaw/plugin-sdk/channel-core";
import { createPluginRuntimeStore } from "openclaw/plugin-sdk/runtime-store";
import { resolveInboundRouteEnvelopeBuilderWithRuntime } from "openclaw/plugin-sdk/inbound-envelope";
import { halPlugin } from "./channel.js";
import type { HalInboundMessage, HalOutboundMessage } from "./types.js";

type PluginRuntime = any;

const { setRuntime, getRuntime } = createPluginRuntimeStore<PluginRuntime>({
  pluginId: "hal",
  errorMessage: "HAL runtime not initialized",
});

function readBody(req: any): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: any[] = [];
    req.on("data", (c: any) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

async function handleHalInbound(cfg: any, message: HalInboundMessage) {
  const runtime = getRuntime();
  const senderId = message.sender_id ?? "kiosk-user";
  const target = `hal:${senderId}`;

  const { route, buildEnvelope } = resolveInboundRouteEnvelopeBuilderWithRuntime({
    cfg,
    channel: "hal",
    accountId: "default",
    peer: { kind: "direct" as const, id: target },
    runtime: runtime.channel,
    sessionStore: cfg.session?.store,
  });

  const { storePath, body } = buildEnvelope({
    channel: "hal",
    from: senderId,
    timestamp: Date.now(),
    body: message.text,
  });

  const ctxPayload = runtime.channel.reply.finalizeInboundContext({
    Body: body,
    BodyForAgent: message.text,
    RawBody: message.text,
    CommandBody: message.text,
    From: target,
    To: target,
    SessionKey: route.sessionKey,
    AccountId: "default",
    ChatType: "direct",
    ConversationLabel: "HAL Kiosk",
    SenderName: senderId,
    SenderId: senderId,
    Provider: "hal",
    Surface: "hal",
    MessageSid: message.request_id,
    MessageSidFull: message.request_id,
    Timestamp: Date.now(),
    OriginatingChannel: "hal",
    OriginatingTo: target,
    CommandAuthorized: true,
  });

  const halBaseUrl: string =
    (cfg?.channels as Record<string, any>)?.hal?.halBaseUrl ?? "";

  await runtime.channel.turn.runAssembled({
    cfg,
    channel: "hal",
    accountId: "default",
    agentId: route.agentId,
    routeSessionKey: route.sessionKey,
    storePath,
    ctxPayload,
    recordInboundSession: runtime.channel.session.recordInboundSession,
    dispatchReplyWithBufferedBlockDispatcher:
      runtime.channel.reply.dispatchReplyWithBufferedBlockDispatcher,
    delivery: {
      deliver: async (payload: any) => {
        const text: string = payload?.text ?? "";
        if (!text.trim() || !halBaseUrl) return;

        const mediaUrls: string[] = [];
        if (Array.isArray(payload.mediaUrls)) mediaUrls.push(...payload.mediaUrls);
        if (typeof payload.mediaUrl === "string" && payload.mediaUrl)
          mediaUrls.push(payload.mediaUrl);

        const outbound: HalOutboundMessage = {
          request_id: message.request_id,
          text,
          media_urls: mediaUrls,
        };

        try {
          const resp = await fetch(`${halBaseUrl}/api/openclaw/response`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(outbound),
          });
          if (!resp.ok) {
            console.error(`[hal] Delivery failed: ${resp.status}`);
          } else {
            console.log(`[hal] Delivered: ${text.length} chars`);
          }
        } catch (err: any) {
          console.error(`[hal] Delivery error: ${err.message}`);
        }
      },
      onError: (error: any) => {
        console.error("[hal] Dispatch error:", error);
        if (halBaseUrl) {
          fetch(`${halBaseUrl}/api/openclaw/response`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              request_id: message.request_id,
              text: "Sorry, I encountered an error processing that request.",
              media_urls: [],
            } satisfies HalOutboundMessage),
          }).catch(() => {});
        }
      },
    },
    replyOptions: {},
    replyPipeline: {},
    record: {
      onRecordError: (error: any) => {
        console.error("[hal] Session record error:", error);
      },
    },
  });
}

export default defineChannelPluginEntry({
  id: "hal",
  name: "HAL Kiosk",
  description: "Channel plugin for HAL voice assistant kiosk",
  plugin: halPlugin,

  setRuntime(runtime: any) {
    setRuntime(runtime);
    console.log("[hal] Runtime injected");
  },

  registerFull(api: any) {
    api.registerHttpRoute({
      path: "/channels/hal/webhook",
      auth: "plugin",
      handler: async (req: any, res: any) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end("Method Not Allowed");
          return true;
        }

        let body: HalInboundMessage;
        try {
          const raw = await readBody(req);
          body = JSON.parse(raw);
        } catch {
          res.statusCode = 400;
          res.end("Bad Request");
          return true;
        }

        if (!body.text || !body.request_id) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "text and request_id required" }));
          return true;
        }

        const runtime = getRuntime();
        const cfg = runtime.config.current();

        res.statusCode = 200;
        res.setHeader("Content-Type", "application/json");
        res.end(
          JSON.stringify({ status: "accepted", request_id: body.request_id })
        );

        console.log(`[hal] Processing: "${body.text.slice(0, 60)}"`);
        handleHalInbound(cfg, body).catch((err: any) => {
          console.error("[hal] handleHalInbound error:", err.message ?? err);
        });

        return true;
      },
    });

    console.log("[hal] Registered HTTP webhook at /channels/hal/webhook");
  },
}) as any;
