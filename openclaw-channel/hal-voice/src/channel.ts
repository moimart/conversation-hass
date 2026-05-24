/**
 * HAL Voice channel plugin definition.
 *
 * Uses createChatChannelPlugin from the OpenClaw SDK with:
 * - Simple account resolution (just the HAL server URL)
 * - Open DM policy (the kiosk is a single-user device)
 * - Outbound sendText that POSTs to HAL's /api/speak
 */

import { createChatChannelPlugin } from "openclaw/plugin-sdk/channel-core";
import { createChannelPluginBase } from "openclaw/plugin-sdk/channel-core";

interface HalVoiceAccount {
  accountId: string | null;
  halServerUrl: string;
  dmPolicy: string;
  allowFrom: string[];
  enabled: boolean;
  configured: boolean;
}

export const halVoicePlugin = createChatChannelPlugin<HalVoiceAccount>({
  base: createChannelPluginBase({
    id: "hal-voice",

    setup: {
      resolveAccount(cfg: any, accountId?: string): HalVoiceAccount {
        const section = cfg?.channels?.["hal-voice"] || {};
        const account = accountId
          ? section?.accounts?.[accountId] || section
          : section;
        return {
          accountId: accountId ?? null,
          halServerUrl: account?.halServerUrl || section?.halServerUrl || "",
          dmPolicy: account?.dmPolicy || section?.dmPolicy || "open",
          allowFrom: account?.allowFrom || section?.allowFrom || [],
          enabled: account?.enabled !== false,
          configured: Boolean(
            account?.halServerUrl || section?.halServerUrl
          ),
        };
      },

      inspectAccount(cfg: any, accountId?: string) {
        const section = cfg?.channels?.["hal-voice"] || {};
        const account = accountId
          ? section?.accounts?.[accountId] || section
          : section;
        const url = account?.halServerUrl || section?.halServerUrl || "";
        return {
          enabled: account?.enabled !== false && Boolean(url),
          configured: Boolean(url),
          halServerUrl: url,
        };
      },
    },
  }),

  // Access control: open by default (kiosk is single-user)
  security: {
    dm: {
      channelKey: "hal-voice",
      resolvePolicy: (account) => account.dmPolicy || "open",
      resolveAllowFrom: (account) => account.allowFrom || [],
      defaultPolicy: "open",
    },
  },

  // Outbound: push agent responses to HAL's REST API for TTS
  outbound: {
    attachedResults: {
      sendText: async (params: any) => {
        const halUrl = resolveHalUrl(params);
        if (!halUrl) {
          return { messageId: "no-hal-url" };
        }

        const text = params.text || params.content || "";
        if (!text.trim()) {
          return { messageId: "empty" };
        }

        try {
          const resp = await fetch(`${halUrl}/api/speak`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: text.trim() }),
          });
          const data = await resp.json().catch(() => ({}));
          return { messageId: data?.id || `hal-${Date.now()}` };
        } catch (err: any) {
          console.error(
            `[hal-voice] Failed to send to HAL: ${err?.message}`
          );
          return { messageId: "error" };
        }
      },
    },
  },
});

function resolveHalUrl(params: any): string {
  // Try to get the HAL URL from the account config
  const account = params?.account;
  if (account?.halServerUrl) return account.halServerUrl.replace(/\/+$/, "");

  // Fallback to channel config
  const cfg = params?.cfg?.channels?.["hal-voice"];
  if (cfg?.halServerUrl) return cfg.halServerUrl.replace(/\/+$/, "");

  return "";
}
