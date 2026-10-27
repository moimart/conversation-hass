// HTTP side of the satellite HA card layer: thin fetch wrappers for the
// server's /api/ha/* routes (server/app/routes_ha.py). Live state does NOT come
// from here — it rides the /ws/ui socket (see hub.ts).

export interface HaState {
  entity_id: string;
  state: string;
  attributes: Record<string, any>;
  last_changed: string;
  last_updated: string;
}

export interface HaBootstrap {
  config: Record<string, any>;
  registry: { entities?: { ei: string; di?: string; ai?: string; en?: string; ic?: string; pl?: string; tk?: string; ec?: number; hn?: boolean }[] };
  resources: { url: string; type: "module" | "js" | "css" | "html" }[];
}

export interface LovelaceCardRef {
  path: string;
  type: string;
  title: string | null;
  card: Record<string, any>;
}

let base = "";
let token = "";

/** Called once at boot with the ACTIVE base (home or gateway) and the token. */
export function configureHaApi(serverBaseUrl: string, deviceToken: string): void {
  base = serverBaseUrl.replace(/\/+$/, "");
  token = deviceToken;
}

export function serverBase(): string {
  return base;
}

function headers(json = false): Record<string, string> {
  return {
    ...(json ? { "Content-Type": "application/json" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function jsonOrThrow(r: Response): Promise<any> {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `${r.status}`);
  return body;
}

const get = (path: string) => fetch(`${base}${path}`, { headers: headers() }).then(jsonOrThrow);
const post = (path: string, body: unknown) =>
  fetch(`${base}${path}`, { method: "POST", headers: headers(true), body: JSON.stringify(body) }).then(jsonOrThrow);

export const haApi = {
  call: (domain: string, service: string, data: Record<string, unknown> = {}, target?: Record<string, unknown>) =>
    post("/api/ha/service", { domain, service, data, target }),
  ws: (msg: Record<string, unknown>) => post("/api/ha/ws", { msg }).then((r) => r.result),
  template: (template: string) => post("/api/ha/template", { template }).then((r) => r.text as string),
  history: (ids: string[], hours: number) =>
    get(`/api/ha/history?e=${ids.join(",")}&hours=${hours}`).then((r) => r.history as { state: string; last_changed: string }[][]),
  forecast: (id: string, type = "daily") =>
    get(`/api/ha/forecast?e=${encodeURIComponent(id)}&type=${type}`).then((r) => r.forecast as Record<string, any>[]),
  entities: (q: string, domain?: string) =>
    get(`/api/ha/entities?q=${encodeURIComponent(q)}${domain ? `&domain=${domain}` : ""}`)
      .then((r) => r.entities as { id: string; name: string; state: string; unit: string | null }[]),
  dashboards: () => get("/api/ha/dashboards").then((r) => r.dashboards as { url_path: string; title: string }[]),
  cards: (dashboard: string) =>
    get(`/api/ha/cards?dashboard=${encodeURIComponent(dashboard)}`).then((r) => r.cards as LovelaceCardRef[]),
  status: () => get("/api/ha/status") as Promise<{ configured: boolean; connected: boolean; entities: number }>,
};

let bootstrap: Promise<HaBootstrap> | null = null;
export function haBootstrap(): Promise<HaBootstrap> {
  bootstrap ||= get("/api/ha/bootstrap").catch((e) => {
    bootstrap = null;
    throw e;
  });
  return bootstrap;
}

/** Authenticated HA images go through the server (the HA token never leaves
 *  it). <img> can't send headers, so the device token rides as ?token=. */
export function haImage(path: string | undefined | null, bust?: number): string | null {
  if (!path) return null;
  if (/^https?:\/\//.test(path)) return path;
  if (path.startsWith("/api/")) {
    return `${base}/api/ha/proxy?path=${encodeURIComponent(path)}&token=${encodeURIComponent(token)}${bust ? `&t=${bust}` : ""}`;
  }
  if (path.startsWith("/local/") || path.startsWith("/hacsfiles/")) return `${base}${path}`;
  return null;
}

// --- per-device layout ---------------------------------------------------------

export interface LayoutItem { id: string; x: number; y: number; w: number; h: number }
export interface SatelliteLayout {
  cards: { id: string; config: Record<string, any> }[];
  portrait: { items: LayoutItem[] };
  landscape: { items: LayoutItem[] };
  version: number;
  /** grid resolution the coordinates are in (1 = first release, 2 = current) */
  res?: number;
}

export const layoutApi = {
  get: () => get("/api/satellite/layout") as Promise<SatelliteLayout>,
  put: (layout: Omit<SatelliteLayout, "version">) =>
    fetch(`${base}/api/satellite/layout`, { method: "PUT", headers: headers(true), body: JSON.stringify(layout) })
      .then(jsonOrThrow) as Promise<SatelliteLayout>,
  reset: () => fetch(`${base}/api/satellite/layout`, { method: "DELETE", headers: headers() }).then(jsonOrThrow),
};
