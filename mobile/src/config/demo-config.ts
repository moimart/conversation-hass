// Reserved default URL + pairing code for App Store review.
//
// Per the plan we use a HOSTED demo server: the app ships these defaults, and an
// App Store reviewer can connect with no real HAL of their own. The user owns
// deploying a public HTTPS/WSS HAL instance with a pre-seeded long-lived demo
// token, then setting DEMO_SERVER_BASE_URL below to it. Until that exists the
// demo path is a no-op (onboarding just prefills the field).
//
// WSS/HTTPS also satisfies iOS ATS for the demo path (no cleartext exception).

export const DEMO_SERVER_BASE_URL = "https://demohal.martinez.sh";
export const DEMO_PAIRING_CODE = "000000";

/** The default URL prefilled on the first onboarding screen. */
export const DEFAULT_SERVER_BASE_URL = DEMO_SERVER_BASE_URL;

/** True when the entered URL is the reserved demo endpoint. */
export function isDemoUrl(url: string): boolean {
  return url.replace(/\/+$/, "") === DEMO_SERVER_BASE_URL.replace(/\/+$/, "");
}
