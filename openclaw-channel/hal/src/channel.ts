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
      sendText: async () => ({ messageId: `hal-${Date.now()}` }),
    },
  },
});
