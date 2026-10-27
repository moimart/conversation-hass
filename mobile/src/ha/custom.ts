// Runs Home Assistant *custom* Lovelace cards (HACS etc.) on a satellite —
// ported from docker-home's custom.tsx.
//
// Custom cards are plain custom elements: HA loads their scripts (the
// "resources" list), then does `el.setConfig(config); el.hass = hass`. We do the
// same. Scripts come from HA through the server's /local + /hacsfiles proxy —
// rewritten to the ACTIVE server base, because the app's own origin is
// capacitor://localhost, not the server. A module's absolute sibling imports
// (`/hacsfiles/x/y.js`) then resolve against the server origin by themselves.
// The `hass` object is backed by the HA bridge (hub.ts + api.ts).

import { haApi, haBootstrap, serverBase, type HaBootstrap, type HaState } from "./api";
import { createCard, openMoreInfo, registerCustomFactory, type CardInstance } from "./cards";
import { haHub } from "./hub";
import { mdiPath } from "./icons";
import { collectEntities, entityIcon, formatState, h, relTime } from "./helpers";

type Cfg = Record<string, any>;

// --- stand-ins for HA frontend elements ----------------------------------------

const SVG_STYLE = `:host{display:inline-flex;align-items:center;justify-content:center;position:relative;vertical-align:middle;
  fill:currentColor;width:var(--mdc-icon-size,24px);height:var(--mdc-icon-size,24px)}svg{width:100%;height:100%;pointer-events:none;display:block}`;

// A fresh subclass per name: a constructor can only be registered once, and
// some stand-ins serve several HA element names.
function defineOnce(name: string, ctor: CustomElementConstructor): void {
  if (!customElements.get(name)) customElements.define(name, class extends (ctor as any) {} as CustomElementConstructor);
}

class SvgIconBase extends HTMLElement {
  protected _path = "";
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `<style>${SVG_STYLE}</style><svg viewBox="0 0 24 24"><path></path></svg>`;
  }
  protected paint(d: string | null): void {
    this.shadowRoot!.querySelector("path")!.setAttribute("d", d || "");
  }
}

class HaSvgIcon extends SvgIconBase {
  static observedAttributes = ["path"];
  get path() { return this._path; }
  set path(v: string) { this._path = v; this.paint(v); }
  attributeChangedCallback(_: string, __: string, v: string) { this.path = v; }
}

class HaIcon extends SvgIconBase {
  static observedAttributes = ["icon"];
  private _icon = "";
  get icon() { return this._icon; }
  set icon(v: string) {
    this._icon = v || "";
    if (!this._icon) { this.paint(null); return; }
    const want = this._icon;
    void mdiPath(want).then((d) => { if (this._icon === want) this.paint(d); });
  }
  attributeChangedCallback(_: string, __: string, v: string) { this.icon = v; }
}

class HaStateIcon extends HaIcon {
  private _stateObj: HaState | null = null;
  private _override: string | undefined;
  static observedAttributes = ["icon"];
  set stateObj(s: HaState | null) { this._stateObj = s; this.refresh(); }
  get stateObj() { return this._stateObj; }
  set hass(_: unknown) { /* not needed */ }
  set stateIcon(v: string) { this._override = v; this.refresh(); }
  attributeChangedCallback(_: string, __: string, v: string) { this._override = v; this.refresh(); }
  private refresh() {
    const id = this._stateObj?.entity_id || "sensor.x";
    super.icon = this._override || entityIcon(this._stateObj, id);
  }
}

class HaCard extends HTMLElement {
  static observedAttributes = ["header"];
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `<style>
      :host{display:block;position:relative;background:var(--ha-card-background,var(--card-background-color,transparent));
        border-radius:var(--ha-card-border-radius,12px);border:var(--ha-card-border-width,0) solid var(--ha-card-border-color,transparent);
        box-shadow:var(--ha-card-box-shadow,none);color:var(--primary-text-color);transition:all .3s ease-out}
      .card-header{font-family:var(--ha-card-header-font-family,inherit);font-size:var(--ha-card-header-font-size,1.2em);
        color:var(--ha-card-header-color,var(--primary-text-color));padding:12px 16px 4px;font-weight:600}
      .card-header:empty{display:none}
    </style><div class="card-header"></div><slot></slot>`;
  }
  set header(v: string) { this.shadowRoot!.querySelector(".card-header")!.textContent = v || ""; }
  attributeChangedCallback(_: string, __: string, v: string) { this.header = v; }
}

class HaRelativeTime extends HTMLElement {
  private t: number | undefined;
  private _dt: string | undefined;
  set datetime(v: string | Date) {
    this._dt = typeof v === "string" ? v : v?.toISOString?.();
    this.render();
  }
  connectedCallback() { this.t = window.setInterval(() => this.render(), 30000); this.render(); }
  disconnectedCallback() { clearInterval(this.t); }
  private render() { this.textContent = relTime(this._dt); }
}

class HaIconButton extends HTMLElement {
  static observedAttributes = ["icon", "path", "label"];
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `<style>
      :host{display:inline-flex;--mdc-icon-size:24px}button{all:unset;cursor:pointer;display:grid;place-items:center;
        width:var(--mdc-icon-button-size,40px);height:var(--mdc-icon-button-size,40px);border-radius:50%;color:inherit}
      button:hover{background:color-mix(in srgb,currentColor 12%,transparent)}
    </style><button><ha-icon></ha-icon></button>`;
  }
  set icon(v: string) { (this.shadowRoot!.querySelector("ha-icon") as HaIcon).icon = v; }
  set path(v: string) {
    const b = this.shadowRoot!.querySelector("button")!;
    b.innerHTML = "";
    const i = document.createElement("ha-svg-icon") as HaSvgIcon;
    i.path = v;
    b.appendChild(i);
  }
  set label(v: string) { this.shadowRoot!.querySelector("button")!.setAttribute("aria-label", v || ""); }
  attributeChangedCallback(n: string, _: string, v: string) { (this as any)[n] = v; }
}

class HaSpinner extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `<style>:host{display:inline-block;width:28px;height:28px}
      div{width:100%;height:100%;border-radius:50%;border:3px solid color-mix(in srgb,var(--primary-color) 25%,transparent);
      border-top-color:var(--primary-color);animation:s 1s linear infinite}@keyframes s{to{transform:rotate(360deg)}}</style><div></div>`;
  }
}

class HaSwitch extends HTMLElement {
  private _checked = false;
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `<style>
      :host{display:inline-block;cursor:pointer}span{display:block;width:36px;height:20px;border-radius:10px;position:relative;
        background:color-mix(in srgb,var(--primary-text-color) 25%,transparent);transition:background .2s}
      span::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .2s}
      :host([checked]) span{background:var(--switch-checked-color,var(--primary-color))}:host([checked]) span::after{transform:translateX(16px)}
    </style><span></span>`;
    this.addEventListener("click", () => {
      this.checked = !this._checked;
      this.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    });
  }
  get checked() { return this._checked; }
  set checked(v: boolean) { this._checked = !!v; this.toggleAttribute("checked", this._checked); }
}

/** A built-in card drawn by PAL, for custom cards that nest others through
 *  loadCardHelpers() (stack-in-card, layout-card, auto-entities…). */
class PalNativeCard extends HTMLElement {
  private inst: CardInstance | null = null;
  private cfg: Cfg | null = null;
  setConfig(c: Cfg) { this.cfg = c; this.render(); }
  set hass(_: unknown) { /* native cards read the HA hub directly */ }
  getCardSize() { return 3; }
  connectedCallback() { this.render(); }
  disconnectedCallback() { this.inst?.destroy(); this.inst = null; }
  private render() {
    if (!this.isConnected || !this.cfg) return;
    this.inst?.destroy();
    this.inst = createCard(this.cfg);
    this.replaceChildren(h("div", { class: "ll-root" }, this.inst.el));
  }
}

let shimsInstalled = false;
function installShims(): void {
  if (shimsInstalled) return;
  shimsInstalled = true;
  defineOnce("ha-svg-icon", HaSvgIcon);
  defineOnce("ha-icon", HaIcon);
  defineOnce("ha-state-icon", HaStateIcon);
  defineOnce("ha-card", HaCard);
  defineOnce("ha-relative-time", HaRelativeTime);
  defineOnce("ha-icon-button", HaIconButton);
  defineOnce("ha-circular-progress", HaSpinner);
  defineOnce("ha-spinner", HaSpinner);
  defineOnce("ha-switch", HaSwitch);
  defineOnce("pal-native-card", PalNativeCard);
  const w = window as any;
  w.customCards ||= [];
  w.loadCardHelpers ||= async () => ({
    createCardElement(config: Cfg) {
      const type = String(config?.type || "");
      if (type.startsWith("custom:") && customElements.get(type.slice(7))) {
        const el = document.createElement(type.slice(7)) as any;
        try { el.setConfig(config); } catch (e) { console.error(e); }
        return el;
      }
      const el = document.createElement("pal-native-card") as PalNativeCard;
      el.setConfig(config);
      return el;
    },
    createRowElement(config: Cfg) {
      return this.createCardElement({ type: "entities", entities: [config] });
    },
    createHuiElement(config: Cfg) {
      return this.createCardElement(config);
    },
  });
}

// --- loading HA's resources (once, on the first custom card) ------------------

/** Resource URLs are server-relative (/hacsfiles/…, /local/…); the app's origin
 *  isn't the server, so anchor them to the active base. */
export function resourceUrl(url: string): string {
  if (/^https?:\/\//.test(url)) return url;
  return `${serverBase()}${url.startsWith("/") ? "" : "/"}${url}`;
}

let resourcesLoaded: Promise<string[]> | null = null;

function loadResources(resources: HaBootstrap["resources"]): Promise<string[]> {
  resourcesLoaded ||= (async () => {
    installShims();
    const failed: string[] = [];
    await Promise.all((resources || []).map((r) => new Promise<void>((resolve) => {
      const url = resourceUrl(r.url);
      if (r.type === "css") {
        const l = document.createElement("link");
        l.rel = "stylesheet";
        l.href = url;
        l.onload = l.onerror = () => resolve();
        document.head.appendChild(l);
        return;
      }
      if (r.type === "html") { resolve(); return; }
      const s = document.createElement("script");
      if (r.type === "module") s.type = "module";
      s.crossOrigin = "anonymous";
      s.src = url;
      s.onload = () => resolve();
      s.onerror = () => { failed.push(r.url); resolve(); };
      document.head.appendChild(s);
    })));
    return failed;
  })();
  return resourcesLoaded;
}

// --- the `hass` object ----------------------------------------------------------

function entityRegistry(b: HaBootstrap): Record<string, any> {
  const out: Record<string, any> = {};
  for (const e of b.registry?.entities || []) {
    out[e.ei] = { entity_id: e.ei, device_id: e.di, area_id: e.ai, name: e.en, icon: e.ic,
      entity_category: e.ec === 0 ? "config" : e.ec === 1 ? "diagnostic" : undefined, hidden: e.hn };
  }
  return out;
}

const ENTITY_RE = /^[a-z_]+\.[a-z0-9_]+$/;

function isLightTheme(): boolean {
  // PAL themes have no light/dark flag; judge by the background luminance.
  const bg = getComputedStyle(document.body).backgroundColor.match(/\d+(\.\d+)?/g);
  if (!bg || bg.length < 3) return false;
  const [r, g, b] = bg.map(Number);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 140;
}

function buildHass(b: HaBootstrap, entities: Record<string, any>, onAccess: (id: string) => void) {
  const snapshot: Record<string, HaState> = {};
  for (const [id, st] of haHub.states) if (st) snapshot[id] = st;
  // Reading an entity we don't hold yet retains it; the card gets it on the
  // next hass update.
  const states = new Proxy(snapshot, {
    get(t, k) {
      if (typeof k === "string" && !(k in t) && ENTITY_RE.test(k)) onAccess(k);
      return Reflect.get(t, k);
    },
  });
  const lang = (navigator.language || "en").split("-")[0];
  const dark = !isLightTheme();
  return {
    states,
    entities,
    devices: {}, areas: {}, floors: {}, services: {}, panels: {},
    config: b.config,
    connected: haHub.connected,
    language: lang,
    selectedLanguage: lang,
    translationMetadata: { fragments: [], translations: { [lang]: { nativeName: lang, isRTL: false }, en: { nativeName: "English", isRTL: false } } },
    locale: { language: lang, number_format: "language", time_format: "language", date_format: "language", first_weekday: "language", time_zone: "local" },
    themes: { darkMode: dark, theme: "pal", default_theme: "default", default_dark_theme: null, themes: {} },
    selectedTheme: { theme: "pal", dark },
    user: { id: "pal", name: "PAL", is_admin: false, is_owner: false, credentials: [], mfa_modules: [] },
    dockedSidebar: "docked",
    vibrate: false,
    debugConnection: false,
    suspendWhenHidden: true,
    enableShortcuts: false,
    localize: (key: string) => (key.split(".").pop() || "").replace(/_/g, " "),
    hassUrl: (path = "") => resourceUrl(path),
    formatEntityState: (s: HaState, state?: string) => formatState(state === undefined ? s : { ...s, state }),
    formatEntityAttributeValue: (s: HaState, attr: string, value?: unknown) => {
      const v = value ?? s?.attributes?.[attr];
      if (attr === "brightness" && typeof v === "number") return `${Math.round((v / 255) * 100)} %`;
      return Array.isArray(v) ? v.join(", ") : v === undefined || v === null ? "" : String(v);
    },
    formatEntityAttributeName: (_: HaState, attr: string) => attr.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
    formatEntityName: (s: HaState) => s?.attributes?.friendly_name || s?.entity_id,
    callService: (domain: string, service: string, data?: Cfg, target?: Cfg) => haApi.call(domain, service, data || {}, target),
    callWS: (msg: Cfg) => haApi.ws(msg),
    callApi: async (method: string, path: string, params?: Cfg) => {
      // The two REST endpoints cards actually use map onto the bridge.
      if (method === "GET" && path.startsWith("history/period")) {
        const ids = new URLSearchParams(path.split("?")[1] || "").get("filter_entity_id")?.split(",") || [];
        return haApi.history(ids, 24);
      }
      if (method === "POST" && path === "template") return haApi.template(String(params?.template || ""));
      throw new Error(`callApi ${method} ${path} is not available on PAL`);
    },
    fetchWithAuth: (path: string, init?: RequestInit) => fetch(resourceUrl(path), init),
    connection: {
      subscribeMessage: (cb: (ev: unknown) => void, msg: Cfg) => haHub.subscribeMessage(cb, msg),
      sendMessagePromise: (msg: Cfg) => haApi.ws(msg),
      subscribeEvents: async () => () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      haVersion: b.config?.version,
    },
  };
}

// --- the card controller ----------------------------------------------------------

function onHaAction(e: Event): void {
  const { config, action } = (e as CustomEvent).detail || {};
  const a = config?.[`${action}_action`] || (action === "tap" ? { action: "more-info" } : null);
  const entity = a?.entity || config?.entity;
  if (!a || a.action === "none") return;
  if (a.action === "more-info" && entity) openMoreInfo(entity);
  else if (a.action === "toggle" && entity) void haApi.call("homeassistant", "toggle", { entity_id: entity }).catch(() => {});
  else if ((a.action === "perform-action" || a.action === "call-service") && (a.perform_action || a.service)) {
    const [d, s] = String(a.perform_action || a.service).split(".");
    void haApi.call(d, s, { ...(a.data || a.service_data || {}) }, a.target).catch(() => {});
  } else if (a.action === "url" && a.url_path) window.open(a.url_path, "_blank", "noopener");
}

function customCard(cfg: Cfg): CardInstance {
  const tag = String(cfg.type).replace(/^custom:/, "");
  const status = h("div", { class: "ll-unsupported" }, `Loading ${tag}…`);
  const host = h("div", { class: "ll-custom-host" });
  const el = h("div", { class: "ll-custom" }, status, host);
  let alive = true;
  let card: any = null;
  let boot: HaBootstrap | null = null;
  let entities: Record<string, any> = {};
  const extra = new Set<string>();
  let releaseExtra: (() => void) | null = null;

  // Entities the config mentions (any string that looks like an entity id)
  // plus those the card reads from hass.states at runtime.
  const ids = new Set<string>([...collectEntities(cfg)]);
  const walk = (v: unknown) => {
    if (typeof v === "string" && ENTITY_RE.test(v)) ids.add(v);
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  walk(cfg);
  const release = haHub.retain([...ids]);

  // Push a fresh hass (new identity so Lit-based cards notice), at most once a frame.
  let frame = 0;
  const push = () => {
    if (frame || !card || !boot) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!card || !boot) return;
      const pending: string[] = [];
      card.hass = buildHass(boot, entities, (id) => { if (!extra.has(id) && !ids.has(id)) pending.push(id); });
      if (pending.length) {
        pending.forEach((id) => extra.add(id));
        releaseExtra?.();
        releaseExtra = haHub.retain([...extra]);
      }
    });
  };
  const unwatch = haHub.watchAll(push);

  host.addEventListener("hass-more-info", (e) => {
    const id = (e as CustomEvent).detail?.entityId;
    if (id) openMoreInfo(id);
  });
  host.addEventListener("hass-action", onHaAction);

  const start = async (attempt = 0): Promise<void> => {
    try {
      boot = await haBootstrap();
    } catch {
      // HA (or the bridge) not up yet — keep trying quietly.
      if (alive) {
        status.textContent = "Waiting for Home Assistant…";
        setTimeout(() => { if (alive) void start(attempt + 1); }, Math.min(30000, 2000 * (attempt + 1)));
      }
      return;
    }
    if (!alive) return;
    entities = entityRegistry(boot);
    const failed = await loadResources(boot.resources);
    if (!customElements.get(tag)) {
      const defined = await Promise.race([
        customElements.whenDefined(tag).then(() => true),
        new Promise<boolean>((r) => setTimeout(() => r(false), 8000)),
      ]);
      if (!defined) {
        status.textContent = failed.length
          ? `“${tag}” didn't load (${failed.length} Home Assistant resource(s) failed: ${failed.join(", ")})`
          : `No Home Assistant resource defines “${tag}”. Is the card installed in HA (Settings → Dashboards → Resources)?`;
        return;
      }
    }
    if (!alive) return;
    const node = document.createElement(tag) as any;
    try {
      node.setConfig?.(structuredClone(cfg));
    } catch (e) {
      status.textContent = `Card config error: ${(e as Error).message}`;
      return;
    }
    status.remove();
    host.replaceChildren(node);
    card = node;
    push();
  };
  void start();

  return {
    el,
    destroy() {
      alive = false;
      cancelAnimationFrame(frame);
      unwatch();
      release();
      releaseExtra?.();
      card?.remove();
      card = null;
    },
  };
}

registerCustomFactory(customCard);
