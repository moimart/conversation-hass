// Entity helpers for the card layer — ported from docker-home's
// src/widgets/lovelace/helpers.tsx (logic unchanged; React bits removed).

import { haApi, type HaState } from "./api";

export const domainOf = (id: string) => id.split(".")[0];

const TOGGLE_DOMAINS = new Set(["light", "switch", "fan", "input_boolean", "automation", "script", "cover", "lock", "media_player", "climate", "humidifier", "siren", "vacuum", "group"]);
export const canToggle = (id: string) => TOGGLE_DOMAINS.has(domainOf(id));

const ACTIVE = new Set(["on", "open", "opening", "unlocked", "playing", "home", "heat", "cool", "heat_cool", "auto", "dry", "fan_only", "cleaning", "active", "armed_home", "armed_away"]);
export const isActive = (s?: HaState | null) => !!s && ACTIVE.has(s.state);
export const isUnavailable = (s?: HaState | null) => !s || s.state === "unavailable" || s.state === "unknown";

export function entityName(s: HaState | null | undefined, id: string, override?: string): string {
  return override || s?.attributes.friendly_name || id.split(".")[1]?.replace(/_/g, " ") || id;
}

const DEVICE_CLASS_ICONS: Record<string, string> = {
  temperature: "thermometer", humidity: "water-percent", battery: "battery", power: "flash", energy: "lightning-bolt",
  illuminance: "brightness-5", pressure: "gauge", co2: "molecule-co2", pm25: "blur", voltage: "sine-wave", current: "current-ac",
  motion: "motion-sensor", occupancy: "home-account", door: "door", window: "window-open-variant", moisture: "water",
  smoke: "smoke-detector", connectivity: "lan-connect", plug: "power-plug", timestamp: "clock-outline", gas: "meter-gas",
  signal_strength: "wifi", speed: "speedometer", duration: "timer-outline", monetary: "cash", data_size: "database",
};
const DOMAIN_ICONS: Record<string, [string, string?]> = {
  light: ["lightbulb", "lightbulb-outline"], switch: ["toggle-switch-variant", "toggle-switch-variant-off"], fan: ["fan", "fan-off"],
  input_boolean: ["toggle-switch-variant", "toggle-switch-variant-off"], automation: ["robot", "robot-off"], script: ["script-text"],
  cover: ["window-shutter-open", "window-shutter"], lock: ["lock-open-variant", "lock"], media_player: ["cast-connected", "cast"],
  climate: ["thermostat"], sensor: ["eye"], binary_sensor: ["checkbox-marked-circle", "radiobox-blank"], person: ["account"],
  device_tracker: ["account"], sun: ["white-balance-sunny"], weather: ["weather-partly-cloudy"], camera: ["cctv"], scene: ["palette"],
  vacuum: ["robot-vacuum"], alarm_control_panel: ["shield-home"], input_number: ["ray-vertex"], input_select: ["format-list-bulleted"],
  number: ["ray-vertex"], select: ["format-list-bulleted"], button: ["gesture-tap-button"], update: ["package-up"], zone: ["map-marker"],
  humidifier: ["air-humidifier", "air-humidifier-off"], timer: ["timer-outline"], counter: ["counter"], calendar: ["calendar"],
  siren: ["bullhorn"], water_heater: ["water-boiler"], remote: ["remote"], event: ["bell-ring-outline"],
};

export function entityIcon(s: HaState | null | undefined, id: string, override?: string): string {
  if (override) return override;
  if (s?.attributes.icon) return s.attributes.icon as string;
  const dc = s?.attributes.device_class as string | undefined;
  if (dc && DEVICE_CLASS_ICONS[dc] && domainOf(id) !== "binary_sensor") return `mdi:${DEVICE_CLASS_ICONS[dc]}`;
  const [on, off] = DOMAIN_ICONS[domainOf(id)] || ["help-circle-outline"];
  return `mdi:${isActive(s) || !off ? on : off}`;
}

export function formatState(s: HaState | null | undefined, opts: { precision?: number } = {}): string {
  if (!s) return "—";
  if (s.state === "unavailable") return "Unavailable";
  if (s.state === "unknown") return "—";
  const unit = s.attributes.unit_of_measurement as string | undefined;
  const n = Number(s.state);
  if (s.state !== "" && Number.isFinite(n) && unit !== undefined) {
    const digits = opts.precision ?? (Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2);
    const v = n.toLocaleString(undefined, { maximumFractionDigits: digits });
    return unit ? `${v}${unit === "%" || unit.startsWith("°") ? "" : " "}${unit}` : v;
  }
  if (s.attributes.device_class === "timestamp" && !Number.isNaN(Date.parse(s.state))) {
    return new Date(s.state).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  }
  const dc = s.attributes.device_class as string | undefined;
  if (domainOf(s.entity_id) === "binary_sensor") {
    const map: Record<string, [string, string]> = {
      door: ["Open", "Closed"], window: ["Open", "Closed"], garage_door: ["Open", "Closed"], opening: ["Open", "Closed"],
      motion: ["Detected", "Clear"], occupancy: ["Detected", "Clear"], presence: ["Home", "Away"], moisture: ["Wet", "Dry"],
      connectivity: ["Connected", "Disconnected"], lock: ["Unlocked", "Locked"], battery: ["Low", "Normal"], problem: ["Problem", "OK"],
      smoke: ["Detected", "Clear"], plug: ["Plugged in", "Unplugged"],
    };
    const pair = (dc && map[dc]) || ["On", "Off"];
    return s.state === "on" ? pair[0] : pair[1];
  }
  return s.state.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function relTime(iso?: string): string {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function entityColor(s: HaState | null | undefined): string | undefined {
  if (!s || !isActive(s)) return undefined;
  const rgb = s.attributes.rgb_color as number[] | undefined;
  if (domainOf(s.entity_id) === "light" && rgb) return `rgb(${rgb.join(",")})`;
  return undefined;
}

// --- actions (tap_action etc.) ----------------------------------------------

export type Action = {
  action?: string;
  service?: string;
  perform_action?: string;
  data?: Record<string, unknown>;
  service_data?: Record<string, unknown>;
  target?: Record<string, unknown>;
  url_path?: string;
  confirmation?: unknown;
};

export async function runAction(
  action: Action | undefined,
  entityId: string | undefined,
  fallback: "toggle" | "more-info" | "none",
  moreInfo: (id: string) => void,
): Promise<unknown> {
  const kind = action?.action || fallback;
  if (kind === "none") return;
  if (action?.confirmation && !confirm("Are you sure?")) return;
  if (kind === "toggle" && entityId) {
    const d = domainOf(entityId);
    if (d === "scene" || d === "script") return haApi.call(d, "turn_on", { entity_id: entityId });
    if (d === "button" || d === "input_button") return haApi.call(d, "press", { entity_id: entityId });
    if (d === "lock") return haApi.call("lock", "toggle", { entity_id: entityId }).catch(() => {});
    return haApi.call("homeassistant", "toggle", { entity_id: entityId });
  }
  if (kind === "call-service" || kind === "perform-action") {
    const svc = action?.perform_action || action?.service;
    if (!svc) return;
    const [domain, service] = svc.split(".");
    // target goes separately (HA merges it), same as the custom-card path.
    return haApi.call(domain, service, { ...(action?.service_data || {}), ...(action?.data || {}) },
      action?.target && Object.keys(action.target).length ? action.target : undefined);
  }
  if (kind === "url" && action?.url_path) {
    window.open(action.url_path, "_blank", "noopener");
    return;
  }
  if ((kind === "more-info" || kind === "navigate") && entityId) moreInfo(entityId);
}

/** Every entity id a card config references (for subscriptions). */
export function collectEntities(cfg: unknown, out = new Set<string>()): Set<string> {
  if (!cfg || typeof cfg !== "object") return out;
  if (Array.isArray(cfg)) {
    for (const c of cfg) {
      if (typeof c === "string" && /^[a-z_]+\.[\w-]+$/.test(c)) out.add(c);
      else collectEntities(c, out);
    }
    return out;
  }
  for (const [k, v] of Object.entries(cfg as Record<string, unknown>)) {
    if ((k === "entity" || k === "camera_image") && typeof v === "string") out.add(v);
    else if (k === "entities" || k === "cards" || k === "card" || k === "conditions" || k === "elements") collectEntities(v, out);
  }
  return out;
}

export function normEntities(list: unknown): { entity: string; name?: string; icon?: string; [k: string]: any }[] {
  if (!Array.isArray(list)) return [];
  return list
    .map((e) => (typeof e === "string" ? { entity: e } : e && typeof e === "object" ? (e as any) : null))
    .filter((e): e is { entity: string } => !!e && (typeof e.entity === "string" || e.type === "section" || e.type === "divider"));
}

/** History series for sparklines / graphs, refreshed every 5 min.
 *  Returns a stop function. */
export function loadHistory(
  ids: string[], hours: number, onData: (series: { t: number; v: number }[][]) => void,
): () => void {
  if (!ids.length) return () => {};
  let alive = true;
  const load = () => haApi.history(ids, hours)
    .then((h) => {
      if (!alive) return;
      onData(ids.map((id) => {
        const rows = h.find((r) => (r[0] as any)?.entity_id === id) || [];
        return rows.map((r) => ({ t: Date.parse(r.last_changed), v: Number(r.state) })).filter((p) => Number.isFinite(p.v));
      }));
    })
    .catch(() => alive && onData([]));
  void load();
  const t = setInterval(load, 5 * 60 * 1000);
  return () => { alive = false; clearInterval(t); };
}

/** Tiny DOM builder: h("div", {class: "x", onclick}, child, "text"). */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K, props: Record<string, any> | null = null, ...children: (Node | string | null | undefined | false)[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sv == null) continue;
        if (sk.startsWith("--")) el.style.setProperty(sk, String(sv));
        else (el.style as any)[sk] = sv;
      }
    } else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k in el && typeof v !== "string") (el as any)[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    el.append(c);
  }
  return el;
}
