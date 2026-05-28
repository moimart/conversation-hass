import { createChatChannelPlugin } from "openclaw/plugin-sdk/channel-core";
import { getChatChannelMeta } from "openclaw/plugin-sdk/channel-plugin-common";
import type { HalResolvedAccount } from "./types.js";

const CHANNEL_ID = "hal" as const;
const meta = { ...getChatChannelMeta(CHANNEL_ID) };

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
  if (!text.trim() && mediaUrls.length === 0) return { messageId };

  try {
    const resp = await fetch(`${account.halBaseUrl}/api/openclaw/say`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, media_urls: mediaUrls }),
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
