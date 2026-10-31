import { createChatChannelPlugin } from "openclaw/plugin-sdk/channel-core";
import { inlineMediaList } from "./media.js";
import type { HalResolvedAccount } from "./types.js";

const CHANNEL_ID = "hal" as const;
// A third-party channel declares its own metadata. (getChatChannelMeta only
// knows OpenClaw's BUNDLED channels; since 2026.10 it throws for any other id.)
const meta = {
  id: CHANNEL_ID,
  label: "PAL",
  selectionLabel: "PAL kiosk",
  blurb: "Voice assistant kiosk — speaks agent replies aloud and shows media on the orb.",
  docsPath: "https://github.com/moimart/conversation-hass/tree/main/openclaw-channel/hal",
  markdownCapable: false,
};

function resolveHalAccount(
  cfg: any,
  accountId?: string | null
): HalResolvedAccount {
  const section = (cfg?.channels as Record<string, any>)?.hal ?? {};
  return {
    accountId: accountId ?? "default",
    halBaseUrl: section.halBaseUrl ?? "",
    halApiKey: section.halApiKey,
    dmPolicy: section.dmPolicy ?? "open",
    allowFrom: section.allowFrom ?? [],
  };
}

// Proactive outbound: POST agent-initiated text/media to the HAL server's
// /api/openclaw/say endpoint so HAL speaks it without a pending user turn.
async function sendToHal(
  ctx: any,
  text: string,
  mediaUrls: string[]
): Promise<{ messageId: string }> {
  const account = resolveHalAccount(ctx.cfg, ctx.accountId);
  const messageId = `hal-${Date.now()}`;
  if (!account.halBaseUrl) {
    console.error("[hal] sendToHal: halBaseUrl not configured");
    return { messageId };
  }
  // Inline gateway-local files as data: URLs (HAL can't read the gateway fs).
  const inlinedMedia = await inlineMediaList(mediaUrls);
  if (!text.trim() && inlinedMedia.length === 0) return { messageId };

  try {
    const resp = await fetch(`${account.halBaseUrl}/api/openclaw/say`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, media_urls: inlinedMedia }),
    });
    if (!resp.ok) {
      console.error(`[hal] proactive say failed: ${resp.status}`);
    } else {
      console.log(`[hal] proactive say delivered: ${text.length} chars`);
    }
  } catch (err: any) {
    console.error(`[hal] proactive say error: ${err?.message ?? err}`);
  }
  return { messageId };
}

export const halPlugin = createChatChannelPlugin<HalResolvedAccount>({
  base: {
    id: CHANNEL_ID,
    meta,
    capabilities: {
      chatTypes: ["direct"],
    },
    config: {
      listAccountIds: (_cfg: any) => ["default"],
      resolveAccount: resolveHalAccount,
      defaultAccountId: () => "default",
      isConfigured: (account: HalResolvedAccount) => Boolean(account.halBaseUrl),
      resolveAllowFrom: () => [],
    },
    // Account lifecycle. PAL pushes turns to our webhook, so there is no
    // connection to own: the account is "running" while the gateway keeps it
    // started, and stopping just resolves when the gateway aborts it. Without
    // this, 2026.10 reports the channel as configured-but-not-running and the
    // health monitor keeps restarting it.
    gateway: {
      startAccount: async (ctx: any) => {
        const now = Date.now();
        ctx.setStatus?.({ accountId: ctx.accountId, running: true, connected: true, lastStartAt: now });
        ctx.log?.info?.(`[hal] account ${ctx.accountId} ready (webhook /channels/hal/webhook)`);
        await new Promise<void>((resolve) => {
          if (ctx.abortSignal?.aborted) return resolve();
          ctx.abortSignal?.addEventListener("abort", () => resolve(), { once: true });
        });
        ctx.setStatus?.({ accountId: ctx.accountId, running: false, connected: false, lastStopAt: Date.now() });
      },
    },
    messaging: {
      normalizeTarget: (raw: string) => raw,
      inferTargetChatType: () => "direct" as const,
      targetResolver: {
        looksLikeId: () => true,
        hint: "<hal:user>",
      },
    },
  },
  security: {
    dm: {
      channelKey: CHANNEL_ID,
      resolvePolicy: () => "open",
      resolveAllowFrom: () => [],
      defaultPolicy: "open",
    },
  },
  outbound: {
    base: {
      deliveryMode: "direct",
    },
    attachedResults: {
      channel: CHANNEL_ID,
      sendText: async (ctx: any) => {
        const text: string = ctx?.text ?? "";
        const mediaUrls: string[] = [];
        if (typeof ctx?.mediaUrl === "string" && ctx.mediaUrl)
          mediaUrls.push(ctx.mediaUrl);
        return sendToHal(ctx, text, mediaUrls);
      },
      sendMedia: async (ctx: any) => {
        const text: string = ctx?.text ?? "";
        const mediaUrls: string[] = [];
        if (typeof ctx?.mediaUrl === "string" && ctx.mediaUrl)
          mediaUrls.push(ctx.mediaUrl);
        if (Array.isArray(ctx?.mediaUrls))
          for (const u of ctx.mediaUrls) if (typeof u === "string") mediaUrls.push(u);
        return sendToHal(ctx, text, mediaUrls);
      },
    },
  },
});
