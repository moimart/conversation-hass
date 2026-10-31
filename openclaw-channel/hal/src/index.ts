import { defineChannelPluginEntry } from "openclaw/plugin-sdk/channel-core";
import { createPluginRuntimeStore } from "openclaw/plugin-sdk/runtime-store";
import { resolveAgentRoute } from "openclaw/plugin-sdk/routing";
import {
  createChannelInboundEnvelopeBuilderAsync,
  resolveInboundSessionEnvelopeContextAsync,
} from "openclaw/plugin-sdk/channel-inbound";
import { halPlugin } from "./channel.js";
import { inlineMediaList } from "./media.js";
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

async function postToHal(halBaseUrl: string, outbound: HalOutboundMessage): Promise<boolean> {
  try {
    const resp = await fetch(`${halBaseUrl}/api/openclaw/response`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(outbound),
    });
    if (!resp.ok) console.error(`[hal] Delivery failed: ${resp.status}`);
    return resp.ok;
  } catch (err: any) {
    console.error(`[hal] Delivery error: ${err.message}`);
    return false;
  }
}

async function handleHalInbound(cfg: any, message: HalInboundMessage) {
  const runtime = getRuntime();
  const senderId = message.sender_id ?? "kiosk-user";
  const target = `hal:${senderId}`;
  const halBaseUrl: string =
    (cfg?.channels as Record<string, any>)?.hal?.halBaseUrl ?? "";

  // OpenClaw 2026.10 inbound API: resolve the route, then prepare the session
  // facts + envelope builder with the awaited helpers (the callback-based
  // inbound-envelope helpers are deprecated since 2026.9.8).
  const route = resolveAgentRoute({
    cfg,
    channel: "hal",
    accountId: "default",
    peer: { kind: "direct", id: target },
  });
  const { storePath } = await resolveInboundSessionEnvelopeContextAsync({
    cfg,
    agentId: route.agentId,
    sessionKey: route.sessionKey,
  });
  const buildEnvelope = await createChannelInboundEnvelopeBuilderAsync({ cfg, route });
  const body = buildEnvelope({
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
    ConversationLabel: "PAL Kiosk",
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

  // runtime.channel.inbound.dispatchReply replaces the deprecated
  // runPreparedReply + hand-rolled runDispatch closure: core records the
  // session, runs the reply pipeline and calls our delivery adapter once per
  // logical reply payload (and owns message_sending / message_sent hooks).
  await runtime.channel.inbound.dispatchReply({
    channel: "hal",
    accountId: "default",
    routeSessionKey: route.sessionKey,
    storePath,
    ctxPayload,
    recordInboundSession: runtime.channel.session.recordInboundSession,
    record: {
      onRecordError: (error: unknown) => {
        console.error("[hal] Session record error:", error);
      },
    },
    cfg,
    agentId: route.agentId,
    dispatchReplyWithBufferedBlockDispatcher:
      runtime.channel.reply.dispatchReplyWithBufferedBlockDispatcher,
    delivery: {
      observeMessageSent: true,
      deliver: async (payload: any) => {
        const text: string = payload?.text ?? "";
        const rawMedia: string[] = [];
        if (Array.isArray(payload?.mediaUrls)) rawMedia.push(...payload.mediaUrls);
        if (typeof payload?.mediaUrl === "string" && payload.mediaUrl) rawMedia.push(payload.mediaUrl);
        // Inline gateway-local files as data: URLs so the containerized PAL
        // server can use them (it can't read the gateway's filesystem).
        const mediaUrls = await inlineMediaList(rawMedia);

        if (!halBaseUrl || (!text.trim() && mediaUrls.length === 0)) {
          return { visibleReplySent: false, suppression: { reason: "no_visible_result" } };
        }
        const ok = await postToHal(halBaseUrl, { request_id: message.request_id, text, media_urls: mediaUrls });
        if (!ok) throw new Error("PAL server did not accept the reply");
        console.log(`[hal] Delivered: ${text.length} chars`);
        return { visibleReplySent: true, content: text };
      },
      onError: (error: unknown) => {
        console.error("[hal] Dispatch error:", error);
        // The turn failed: answer the pending PAL request so it doesn't hang
        // until its 120 s timeout.
        if (halBaseUrl) {
          void postToHal(halBaseUrl, {
            request_id: message.request_id,
            text: "Sorry, I encountered an error processing that request.",
            media_urls: [],
          });
        }
      },
    },
    replyOptions: {},
  });
}

export default defineChannelPluginEntry({
  id: "hal",
  name: "PAL Kiosk",
  description: "Channel plugin for the PAL voice assistant kiosk",
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

        // OpenClaw 2026.10 admits agent work per request: a turn started after
        // the handler returns is refused ("Gateway is temporarily unavailable").
        // So the turn runs INSIDE the request and we answer when it settles.
        // Replies still reach PAL as they are produced, through its
        // /api/openclaw/response callback (the delivery adapter), so PAL never
        // waits on this HTTP response for the answer itself.
        console.log(`[hal] Processing: "${body.text.slice(0, 60)}"`);
        let status = "completed";
        try {
          await handleHalInbound(cfg, body);
        } catch (err: any) {
          status = "failed";
          console.error("[hal] handleHalInbound error:", err?.message ?? err);
        }
        res.statusCode = status === "completed" ? 200 : 500;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ status, request_id: body.request_id }));

        return true;
      },
    });

    console.log("[hal] Registered HTTP webhook at /channels/hal/webhook");
  },
}) as any;
