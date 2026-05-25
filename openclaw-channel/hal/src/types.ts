export interface HalInboundMessage {
  text: string;
  request_id: string;
  sender_id?: string;
}

export interface HalOutboundMessage {
  request_id: string;
  text: string;
  media_urls: string[];
}

export interface HalChannelConfig {
  halBaseUrl: string;
  halApiKey?: string;
  dmPolicy?: "open" | "allowlist";
  allowFrom?: string[];
}

export interface HalResolvedAccount {
  accountId: string | null;
  halBaseUrl: string;
  halApiKey?: string;
  dmPolicy: string;
  allowFrom: string[];
}
