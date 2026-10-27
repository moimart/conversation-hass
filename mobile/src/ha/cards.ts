// Native renderers for Home Assistant Lovelace card configs — a vanilla-DOM
// port of docker-home's src/widgets/lovelace/cards.tsx. Each card takes the same
// YAML/JSON as a dashboard card and draws it in PAL's theme with live state.
//
// Rendering model (the main change from React): every card is
//   createCard(cfg) → { el, destroy() }
// and re-renders ITSELF only when one of its own entities changes (haHub.watch
// is per-entity). Interactive state that must survive a re-render — a light
// slider mid-drag, a thermostat's pending setpoint, a camera cache-buster —
// lives in the card's closure, and the slider defers re-renders while dragged.

import { haApi, haImage, type HaState } from "./api";
import { haHub } from "./hub";
import { mdiIcon } from "./icons";
import {
  canToggle, domainOf, entityColor, entityIcon, entityName, formatState, h, isActive,
  isUnavailable, loadHistory, normEntities, relTime, runAction,
} from "./helpers";

type Cfg = Record<string, any>;
export interface CardInstance { el: HTMLElement; destroy(): void }

const st = (id: string | undefined): HaState | null | undefined => (id ? haHub.states.get(id) : undefined);
const more = (id: string) => openMoreInfo(id);
const icon = (name: string, size = 24, cls = "") => mdiIcon(name, size, cls);

type Reactive = CardInstance & { hold(b: boolean): void; repaint(): void };

/** Card shell that re-renders `render()` into `el` whenever `ids` change.
 *  `repaint()` re-renders on demand (async data such as history arrived);
 *  `hold(true)` defers re-renders while the user is mid-interaction. */
function reactive(el: HTMLElement, ids: string[], render: () => (Node | string | null | false)[] | Node | null): Reactive {
  let blocked = false;
  let dirty = false;
  const paint = () => {
    if (blocked) { dirty = true; return; }
    const out = render();
    el.replaceChildren(...(Array.isArray(out) ? out.filter((x): x is Node | string => !!x) : out ? [out] : []));
  };
  paint();
  const unwatch = haHub.watch(ids.filter(Boolean), paint);
  return {
    el,
    destroy() { unwatch(); },
    repaint: paint,
    hold(b: boolean) { blocked = b; if (!b && dirty) { dirty = false; paint(); } },
  };
}

const title = (t?: string) => (t ? h("div", { class: "ll-title" }, t) : null);

function unsupported(type?: string): CardInstance {
  const el = h("div", { class: "ll-unsupported" }, icon("mdi:puzzle-outline", 22),
    h("span", null, "Card type ", h("b", { class: "mono" }, type || "?"),
      " isn't supported on PAL yet. Custom cards work with a ", h("span", { class: "mono" }, "custom:"),
      " type as installed in Home Assistant."));
  return { el, destroy() {} };
}

function switchEl(on: boolean, onChange: () => void, disabled = false): HTMLButtonElement {
  return h("button", {
    type: "button", role: "switch", "aria-checked": String(on), class: `ll-switch ${on ? "on" : ""}`,
    disabled, onclick: (e: Event) => { e.stopPropagation(); onChange(); },
  }, h("span"));
}

// --- entity rows / tiles ------------------------------------------------------

function entityRow(row: Cfg): HTMLElement {
  if (row.type === "divider") return h("hr", { class: "ll-divider" });
  if (row.type === "section") return h("div", { class: "ll-section" }, row.label || "");
  const id = row.entity as string;
  const s = st(id);
  const active = isActive(s);
  const toggle = canToggle(id) && !["media_player", "climate", "cover", "lock"].includes(domainOf(id));
  const secondary = row.secondary_info === "last-changed" ? relTime(s?.last_changed)
    : row.secondary_info === "last-updated" ? relTime(s?.last_updated) : null;
  const d = domainOf(id);
  return h("div", { class: `ll-row ${isUnavailable(s) ? "unavail" : ""}` },
    h("button", { type: "button", class: `ll-icon ${active ? "on" : ""}`, style: { color: entityColor(s) },
      onclick: () => runAction(row.tap_action, id, "more-info", more) }, icon(entityIcon(s, id, row.icon))),
    h("button", { type: "button", class: "ll-row-name", onclick: () => more(id) },
      h("span", null, entityName(s, id, row.name)), secondary ? h("small", null, secondary) : null),
    toggle ? switchEl(active, () => runAction({ action: "toggle" }, id, "toggle", more), isUnavailable(s))
      : d === "scene" || d === "script" || d === "button"
        ? h("button", { type: "button", class: "ll-run", onclick: () => runAction({ action: "toggle" }, id, "toggle", more) }, "Run")
        : h("span", { class: "ll-state" }, formatState(s)));
}

function entitiesCard(cfg: Cfg): CardInstance {
  const rows = normEntities(cfg.entities);
  return reactive(h("div", { class: "ll-card" }), rows.map((r) => r.entity),
    () => [title(cfg.title), h("div", { class: "ll-rows" }, ...rows.map(entityRow))]);
}

function entityCard(cfg: Cfg): CardInstance {
  const el = h("button", { type: "button", class: "ll-card ll-entity",
    onclick: () => runAction(cfg.tap_action, cfg.entity, "more-info", more) });
  return reactive(el, [cfg.entity], () => {
    const s = st(cfg.entity);
    const value = cfg.attribute ? s?.attributes[cfg.attribute] : null;
    return [
      h("div", { class: "ll-entity-top" }, h("span", null, entityName(s, cfg.entity, cfg.name)),
        icon(entityIcon(s, cfg.entity, cfg.icon), 24, isActive(s) ? "accent" : "dim")),
      h("div", { class: "ll-big" }, cfg.attribute ? `${value ?? "—"}${cfg.unit ? ` ${cfg.unit}` : ""}` : formatState(s)),
    ];
  });
}

function glanceCard(cfg: Cfg): CardInstance {
  const rows = normEntities(cfg.entities);
  return reactive(h("div", { class: "ll-card" }), rows.map((r) => r.entity), () => [
    title(cfg.title),
    h("div", { class: "ll-glance", style: { gridTemplateColumns: `repeat(${cfg.columns || Math.min(rows.length, 5) || 1}, 1fr)` } },
      ...rows.map((r) => {
        const s = st(r.entity);
        return h("button", { type: "button", class: "ll-glance-item",
          onclick: () => runAction(r.tap_action || cfg.tap_action, r.entity, "more-info", more) },
          cfg.show_name !== false ? h("span", { class: "ll-glance-name" }, entityName(s, r.entity, r.name)) : null,
          cfg.show_icon !== false ? (() => { const i = icon(entityIcon(s, r.entity, r.icon), 24, isActive(s) ? "accent" : ""); const c = entityColor(s); if (c) i.style.color = c; return i; })() : null,
          cfg.show_state !== false ? h("span", { class: "ll-glance-state" }, formatState(s)) : null);
      })),
  ]);
}

function tileCard(cfg: Cfg): CardInstance {
  const id = cfg.entity as string;
  const el = h("div", { class: "ll-card ll-tile" });
  return reactive(el, [id], () => {
    const s = st(id);
    const active = isActive(s);
    el.classList.toggle("on", active);
    const col = entityColor(s);
    if (col) el.style.setProperty("--ent", col); else el.style.removeProperty("--ent");
    const pct = domainOf(id) === "light" && s?.attributes.brightness ? Math.round((s.attributes.brightness / 255) * 100) : null;
    return [
      h("button", { type: "button", class: `ll-icon big ${active ? "on" : ""}`, style: { color: col },
        onclick: () => runAction(cfg.icon_tap_action || { action: canToggle(id) ? "toggle" : "more-info" }, id, "toggle", more) },
        icon(entityIcon(s, id, cfg.icon))),
      h("button", { type: "button", class: "ll-tile-text", onclick: () => runAction(cfg.tap_action, id, "more-info", more) },
        h("strong", null, entityName(s, id, cfg.name)),
        h("span", null, `${formatState(s)}${pct !== null && active ? ` · ${pct}%` : ""}`)),
    ];
  });
}

function buttonCard(cfg: Cfg): CardInstance {
  const fallback = cfg.entity && canToggle(cfg.entity) ? "toggle" : "more-info";
  const el = h("button", { type: "button", class: "ll-card ll-button",
    onclick: () => runAction(cfg.tap_action, cfg.entity, fallback, more) });
  return reactive(el, cfg.entity ? [cfg.entity] : [], () => {
    const s = st(cfg.entity);
    el.classList.toggle("on", isActive(s));
    const size = cfg.icon_height ? parseInt(cfg.icon_height, 10) || 48 : 48;
    const i = cfg.show_icon !== false ? icon(entityIcon(s, cfg.entity || "button.x", cfg.icon), size) : null;
    const col = entityColor(s);
    if (i && col) i.style.color = col;
    return [
      i,
      cfg.show_name !== false ? h("strong", null, cfg.name || (cfg.entity ? entityName(s, cfg.entity) : "")) : null,
      cfg.show_state ? h("span", { class: "dim" }, formatState(s)) : null,
    ];
  });
}

// --- numbers & charts ---------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";
let gradSeq = 0;
function svg(tag: string, attrs: Record<string, string | number>): SVGElement {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
}

function sparkline(series: { t: number; v: number }[][], height = 60, colors?: string[], fill = false): Element {
  const all = series.flat();
  if (all.length < 2) return h("div", { class: "ll-spark-empty", style: { height: `${height}px` } });
  const t0 = Math.min(...all.map((p) => p.t)), t1 = Math.max(Date.now(), ...all.map((p) => p.t));
  let v0 = Math.min(...all.map((p) => p.v)), v1 = Math.max(...all.map((p) => p.v));
  if (v0 === v1) { v0 -= 1; v1 += 1; }
  const W = 300;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * W;
  const y = (v: number) => height - 4 - ((v - v0) / (v1 - v0)) * (height - 8);
  const gid = `ll-fill-${++gradSeq}`; // unique per chart (docker-home reused one id)
  const root = svg("svg", { class: "ll-spark", viewBox: `0 0 ${W} ${height}`, preserveAspectRatio: "none" });
  (root as SVGElement).setAttribute("style", fill ? "flex:1;min-height:24px;height:auto" : `height:${height}px`);
  const defs = svg("defs", {});
  const grad = svg("linearGradient", { id: gid, x1: 0, x2: 0, y1: 0, y2: 1 });
  grad.append(svg("stop", { offset: 0, "stop-color": "var(--accent)", "stop-opacity": 0.35 }),
    svg("stop", { offset: 1, "stop-color": "var(--accent)", "stop-opacity": 0 }));
  defs.append(grad);
  root.append(defs);
  series.forEach((pts, i) => {
    if (pts.length < 2) return;
    const last = pts[pts.length - 1];
    const line = pts.map((p, j) => `${j ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("") + `L${W},${y(last.v).toFixed(1)}`;
    if (i === 0) root.append(svg("path", { d: `${line}L${W},${height}L${x(pts[0].t)},${height}Z`, fill: `url(#${gid})` }));
    root.append(svg("path", { d: line, fill: "none", stroke: colors?.[i] || (i ? "var(--accent-warm)" : "var(--accent)"),
      "stroke-width": 2, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
  });
  return root;
}

function sensorCard(cfg: Cfg): CardInstance {
  let hist: { t: number; v: number }[][] | null = null;
  const el = h("button", { type: "button", class: "ll-card ll-sensor", onclick: () => more(cfg.entity) });
  const inst = reactive(el, [cfg.entity], () => {
    const s = st(cfg.entity);
    return [
      h("div", { class: "ll-entity-top" }, h("span", null, entityName(s, cfg.entity, cfg.name)),
        icon(entityIcon(s, cfg.entity, cfg.icon), 24, "dim")),
      h("div", { class: "ll-big" }, formatState(s)),
      cfg.graph !== "none" && hist ? sparkline(hist, 60, undefined, true) : null,
    ];
  });
  const stop = cfg.graph === "none" ? () => {} : loadHistory([cfg.entity], cfg.hours_to_show || 24, (d) => {
    hist = d; repaint(inst);
  });
  return withCleanup(inst, stop);
}

const PALETTE = ["var(--accent)", "var(--accent-warm)", "var(--accent-glow)", "var(--success)", "var(--text-dim)"];

function historyGraphCard(cfg: Cfg): CardInstance {
  const rows = normEntities(cfg.entities);
  const ids = rows.map((r) => r.entity);
  let hist: { t: number; v: number }[][] | null = null;
  const inst = reactive(h("div", { class: "ll-card" }), ids, () => [
    title(cfg.title),
    h("div", { class: "ll-legend" }, ...rows.map((r, i) => h("span", null,
      h("i", { style: { background: PALETTE[i % PALETTE.length] } }),
      `${entityName(st(r.entity), r.entity, r.name)} `, h("b", null, formatState(st(r.entity)))))),
    hist ? sparkline(hist, 110, PALETTE, true) : null,
  ]);
  return withCleanup(inst, loadHistory(ids, cfg.hours_to_show || 24, (d) => { hist = d; repaint(inst); }));
}

function gaugeCard(cfg: Cfg): CardInstance {
  const el = h("button", { type: "button", class: "ll-card ll-gauge", onclick: () => more(cfg.entity) });
  return reactive(el, [cfg.entity], () => {
    const s = st(cfg.entity);
    const min = Number(cfg.min ?? 0), max = Number(cfg.max ?? 100);
    const v = Number(s?.state);
    const frac = Number.isFinite(v) ? Math.max(0, Math.min(1, (v - min) / (max - min))) : 0;
    const sev = cfg.severity as { green?: number; yellow?: number; red?: number } | undefined;
    let color = "var(--accent)";
    if (sev && Number.isFinite(v)) {
      const steps = (["red", "yellow", "green"] as const).filter((k) => sev[k] !== undefined)
        .map((k) => [k, sev[k]!] as const).sort((a, b) => b[1] - a[1]);
      const hit = steps.find(([, lim]) => v >= lim);
      color = hit ? { green: "#34c759", yellow: "#ffb020", red: "#ff4d4d" }[hit[0]] : color;
    }
    const c = Math.PI * 40;
    const root = svg("svg", { viewBox: "0 0 100 58" });
    root.append(
      svg("path", { d: "M10,50 A40,40 0 0 1 90,50", fill: "none", stroke: "color-mix(in srgb, var(--text) 12%, transparent)", "stroke-width": 9, "stroke-linecap": "round" }),
      svg("path", { d: "M10,50 A40,40 0 0 1 90,50", fill: "none", stroke: color, "stroke-width": 9, "stroke-linecap": "round", "stroke-dasharray": `${c * frac} ${c}` }),
    );
    const val = svg("text", { x: 50, y: 46, "text-anchor": "middle", class: "ll-gauge-value" });
    val.textContent = Number.isFinite(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "—";
    const unit = svg("text", { x: 50, y: 56, "text-anchor": "middle", class: "ll-gauge-unit" });
    unit.textContent = String(cfg.unit ?? s?.attributes.unit_of_measurement ?? "");
    root.append(val, unit);
    return [root, h("span", { class: "ll-gauge-name" }, entityName(s, cfg.entity, cfg.name))];
  });
}

// --- rich cards ---------------------------------------------------------------

export function mdToHtml(src: string): string {
  const esc = src.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (t: string) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out: string[] = [];
  let list: string | null = null;
  for (const line of esc.split("\n")) {
    const hd = line.match(/^(#{1,4})\s+(.*)/);
    const li = line.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (li) {
      const kind = /\d/.test(li[1]) ? "ol" : "ul";
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(li[2])}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    if (hd) out.push(`<h${hd[1].length + 2}>${inline(hd[2])}</h${hd[1].length + 2}>`);
    else if (line.trim() === "") out.push("<br/>");
    else out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list}>`);
  return out.join("");
}

function markdownCard(cfg: Cfg): CardInstance {
  const content = String(cfg.content || "");
  const templated = /\{\{|\{%/.test(content);
  const body = h("div");
  const el = h("div", { class: "ll-card ll-markdown" }, title(cfg.title), body);
  const set = (t: string) => { body.innerHTML = mdToHtml(t); };
  if (!templated) { set(content); return { el, destroy() {} }; }
  let alive = true;
  const load = () => haApi.template(content).then((t) => alive && set(t)).catch((e) => alive && set(`⚠ ${e.message}`));
  void load();
  const t = setInterval(load, 30000);
  return { el, destroy() { alive = false; clearInterval(t); } };
}

const WEATHER_ICONS: Record<string, string> = {
  "clear-night": "weather-night", cloudy: "weather-cloudy", exceptional: "alert-circle-outline", fog: "weather-fog", hail: "weather-hail",
  lightning: "weather-lightning", "lightning-rainy": "weather-lightning-rainy", partlycloudy: "weather-partly-cloudy", pouring: "weather-pouring",
  rainy: "weather-rainy", snowy: "weather-snowy", "snowy-rainy": "weather-snowy-rainy", sunny: "weather-sunny", windy: "weather-windy", "windy-variant": "weather-windy-variant",
};

function weatherCard(cfg: Cfg): CardInstance {
  let fc: Cfg[] = [];
  const type = cfg.forecast_type || "daily";
  const inst = reactive(h("div", { class: "ll-card ll-weather" }), [cfg.entity], () => {
    const s = st(cfg.entity);
    const a = s?.attributes || {};
    const unit = a.temperature_unit || "°";
    return [
      h("button", { type: "button", class: "ll-weather-now", onclick: () => more(cfg.entity) },
        icon(`mdi:${WEATHER_ICONS[s?.state || ""] || "weather-partly-cloudy"}`, 56, "accent"),
        h("div", null,
          h("div", { class: "ll-big" }, String(a.temperature ?? "—"), h("small", null, unit)),
          h("div", { class: "dim" }, `${cfg.name || formatState(s)}${a.humidity !== undefined ? ` · ${a.humidity}%` : ""}`))),
      fc.length ? h("div", { class: "ll-forecast" }, ...fc.slice(0, cfg.forecast_slots || 5).map((f) => h("div", null,
        h("small", null, new Date(f.datetime).toLocaleString([], type === "hourly" ? { hour: "2-digit" } : { weekday: "short" })),
        icon(`mdi:${WEATHER_ICONS[f.condition] || "weather-partly-cloudy"}`, 22),
        h("span", null, `${Math.round(f.temperature)}°`,
          f.templow !== undefined ? h("span", { class: "dim" }, ` ${Math.round(f.templow)}°`) : null)))) : null,
    ];
  });
  if (cfg.show_forecast === false || !cfg.entity) return inst;
  let alive = true;
  const load = () => haApi.forecast(cfg.entity, type).then((f) => { if (alive) { fc = f; repaint(inst); } }).catch(() => {});
  void load();
  const t = setInterval(load, 30 * 60 * 1000);
  return withCleanup(inst, () => { alive = false; clearInterval(t); });
}

function thermostatCard(cfg: Cfg): CardInstance {
  let pending: number | null = null;
  let timer: number | undefined;
  const inst = reactive(h("div", { class: "ll-card ll-thermo" }), [cfg.entity], () => {
    const s = st(cfg.entity);
    const a = s?.attributes || {};
    const step = Number(a.target_temp_step) || (a.temperature_unit === "°F" ? 1 : 0.5);
    const target = pending ?? (a.temperature as number | undefined);
    const nudge = (d: number) => {
      if (target === undefined) return;
      const next = Math.round((target + d * step) / step) * step;
      const clamped = Math.max(a.min_temp ?? -100, Math.min(a.max_temp ?? 100, next));
      pending = clamped;
      repaint(inst);
      clearTimeout(timer);
      timer = window.setTimeout(() => {
        haApi.call("climate", "set_temperature", { entity_id: cfg.entity, temperature: clamped })
          .catch(() => {}).finally(() => { pending = null; repaint(inst); });
      }, 700);
    };
    const action = a.hvac_action as string | undefined;
    return [
      title(cfg.name || entityName(s, cfg.entity)),
      h("div", { class: `ll-thermo-dial ${action || s?.state || ""}` },
        h("div", { class: "ll-big" }, target !== undefined ? Number(target).toFixed(step < 1 ? 1 : 0) : "—",
          h("small", null, a.temperature_unit || "°")),
        h("div", { class: "dim" }, `Now ${a.current_temperature ?? "—"}° · ${(action || s?.state || "").replace(/_/g, " ")}`)),
      h("div", { class: "ll-thermo-btns" },
        h("button", { type: "button", "aria-label": "Lower", onclick: () => nudge(-1) }, icon("mdi:minus")),
        h("button", { type: "button", "aria-label": "Raise", onclick: () => nudge(1) }, icon("mdi:plus"))),
    ];
  });
  return withCleanup(inst, () => clearTimeout(timer));
}

function lightCard(cfg: Cfg): CardInstance {
  let drag: number | null = null;
  let inst: Reactive;
  const commit = () => {
    if (drag === null) return;
    const v = drag;
    haApi.call("light", "turn_on", { entity_id: cfg.entity, brightness_pct: v })
      .catch(() => {}).finally(() => { drag = null; inst.hold(false); });
  };
  inst = reactive(h("div", { class: "ll-card ll-light" }), [cfg.entity], () => {
    const s = st(cfg.entity);
    const on = isActive(s);
    const pct = s?.attributes.brightness ? Math.round((s.attributes.brightness / 255) * 100) : 0;
    const label = h("span", { class: "dim" }, on ? `${drag ?? pct}%` : formatState(s));
    const bulb = h("button", { type: "button", class: `ll-light-bulb ${on ? "on" : ""}`, style: { color: entityColor(s) },
      onclick: () => runAction(cfg.tap_action, cfg.entity, "toggle", more) }, icon(entityIcon(s, cfg.entity, cfg.icon), 44));
    const dimmable = s?.attributes.supported_color_modes?.some((m: string) => m !== "onoff") !== false;
    const range = dimmable ? h("input", { type: "range", min: "1", max: "100", "aria-label": "Brightness" }) : null;
    if (range) {
      range.value = String(drag ?? (on ? pct : 0));
      // Hold re-renders while the thumb is down, so a state update can't yank
      // the slider out from under the finger.
      range.addEventListener("pointerdown", () => inst.hold(true));
      range.addEventListener("input", () => { drag = Number(range.value); label.textContent = `${drag}%`; });
      range.addEventListener("pointerup", commit);
      range.addEventListener("pointercancel", () => { drag = null; inst.hold(false); });
      range.addEventListener("keyup", commit);
    }
    return [bulb, h("strong", null, entityName(s, cfg.entity, cfg.name)), label, range];
  });
  return inst;
}

function mediaCard(cfg: Cfg): CardInstance {
  const el = h("div", { class: "ll-card ll-media" });
  const svc = (service: string) => haApi.call("media_player", service, { entity_id: cfg.entity }).catch(() => {});
  return reactive(el, [cfg.entity], () => {
    const s = st(cfg.entity);
    const a = s?.attributes || {};
    const art = haImage(a.entity_picture);
    if (art) el.style.setProperty("--art", `url("${art}")`); else el.style.removeProperty("--art");
    const playing = s?.state === "playing";
    return [
      art ? h("div", { class: "ll-media-bg" }) : null,
      h("div", { class: "ll-media-body" },
        art ? h("img", { src: art, alt: "" }) : icon("mdi:music-note", 40, "dim"),
        h("div", { class: "ll-media-text" },
          h("strong", null, a.media_title || entityName(s, cfg.entity, cfg.name)),
          h("span", { class: "dim" }, a.media_artist || a.app_name || formatState(s)))),
      h("div", { class: "ll-media-ctl" },
        h("button", { type: "button", "aria-label": "Previous", onclick: () => svc("media_previous_track") }, icon("mdi:skip-previous")),
        h("button", { type: "button", class: "main", "aria-label": "Play/pause", onclick: () => svc("media_play_pause") },
          icon(playing ? "mdi:pause" : "mdi:play", 30)),
        h("button", { type: "button", "aria-label": "Next", onclick: () => svc("media_next_track") }, icon("mdi:skip-next"))),
    ];
  });
}

/** Image source for picture cards: explicit image, a camera (refreshed every
 *  10 s via a cache-buster) or the entity's picture. */
function pictureSource(cfg: Cfg, onTick: () => void): { src: () => string | null; stop: () => void } {
  const cam = cfg.camera_image || (cfg.entity && domainOf(cfg.entity) === "camera" ? cfg.entity : undefined);
  let bust = Date.now();
  const t = cam ? setInterval(() => { bust = Date.now(); onTick(); }, 10000) : undefined;
  return {
    src: () => {
      if (cfg.image) return haImage(cfg.image) || cfg.image;
      if (cam) return haImage(`/api/camera_proxy/${cam}`, bust);
      return haImage(st(cfg.entity)?.attributes.entity_picture);
    },
    stop: () => clearInterval(t),
  };
}

/** Swap an <img> src only once the next frame has loaded (no flash to blank). */
function pictureImg(src: string | null): HTMLElement {
  if (!src) return h("div", { class: "ll-picture-empty" }, icon("mdi:image-off-outline", 36));
  return h("img", { src, alt: "" });
}

function pictureEntityCard(cfg: Cfg): CardInstance {
  const el = h("button", { type: "button", class: "ll-card ll-picture",
    onclick: () => runAction(cfg.tap_action, cfg.entity, "more-info", more) });
  let img: HTMLElement | null = null;
  const pic = pictureSource(cfg, () => { const s = pic.src(); if (img instanceof HTMLImageElement && s) preload(img, s); });
  const inst = reactive(el, [cfg.entity], () => {
    const s = st(cfg.entity);
    img = pictureImg(pic.src());
    return [img, cfg.show_name !== false || cfg.show_state !== false ? h("div", { class: "ll-picture-bar" },
      cfg.show_name !== false ? h("span", null, entityName(s, cfg.entity, cfg.name)) : null,
      cfg.show_state !== false ? h("span", null, formatState(s)) : null) : null];
  });
  return withCleanup(inst, pic.stop);
}

function preload(img: HTMLImageElement, src: string): void {
  const next = new Image();
  next.onload = () => { img.src = src; };
  next.src = src;
}

function pictureGlanceCard(cfg: Cfg): CardInstance {
  const rows = normEntities(cfg.entities);
  let img: HTMLElement | null = null;
  const pic = pictureSource(cfg, () => { const s = pic.src(); if (img instanceof HTMLImageElement && s) preload(img, s); });
  const inst = reactive(h("div", { class: "ll-card ll-picture" }), [cfg.entity, ...rows.map((r) => r.entity)].filter(Boolean), () => {
    img = pictureImg(pic.src());
    return [img, h("div", { class: "ll-picture-bar" }, h("span", null, cfg.title || ""),
      h("span", { class: "ll-picture-icons" }, ...rows.map((r) => {
        const s = st(r.entity);
        return h("button", { type: "button", title: entityName(s, r.entity), class: isActive(s) ? "on" : "",
          onclick: (e: Event) => { e.stopPropagation(); void runAction(r.tap_action, r.entity, canToggle(r.entity) ? "toggle" : "more-info", more); } },
          icon(entityIcon(s, r.entity, r.icon), 20));
      })))];
  });
  return withCleanup(inst, pic.stop);
}

function pictureCard(cfg: Cfg): CardInstance {
  const src = haImage(cfg.image) || cfg.image;
  return { el: h("div", { class: "ll-card ll-picture" }, src ? h("img", { src, alt: "" }) : null), destroy() {} };
}

// --- layout cards -------------------------------------------------------------

function container(cfg: Cfg, cls: string, style?: Record<string, string>): CardInstance {
  const children = ((cfg.cards || []) as Cfg[]).map((c) => createCard(c));
  const el = h("div", { class: "ll-stack-wrap" }, title(cfg.title),
    h("div", { class: cls, style }, ...children.map((c) => c.el)));
  return { el, destroy() { children.forEach((c) => c.destroy()); } };
}

function conditionalCard(cfg: Cfg): CardInstance {
  const conds = (cfg.conditions || []) as Cfg[];
  const el = h("div", { class: "ll-conditional" });
  let child: CardInstance | null = null;
  const pass = () => conds.every((c) => {
    const s = st(c.entity)?.state;
    if (c.state !== undefined) return Array.isArray(c.state) ? c.state.includes(s) : s === String(c.state);
    if (c.state_not !== undefined) return Array.isArray(c.state_not) ? !c.state_not.includes(s) : s !== String(c.state_not);
    if (c.above !== undefined || c.below !== undefined) {
      const n = Number(s);
      return (c.above === undefined || n > c.above) && (c.below === undefined || n < c.below);
    }
    return true;
  });
  const update = () => {
    const ok = pass() && !!cfg.card;
    if (ok && !child) { child = createCard(cfg.card); el.replaceChildren(child.el); }
    else if (!ok && child) { child.destroy(); child = null; el.replaceChildren(); }
  };
  update();
  const unwatch = haHub.watch(conds.map((c) => c.entity).filter(Boolean), update);
  return { el, destroy() { unwatch(); child?.destroy(); } };
}

function headingCard(cfg: Cfg): CardInstance {
  return { el: h("div", { class: "ll-heading" }, cfg.icon ? icon(cfg.icon, 20) : null, cfg.heading || cfg.title || ""), destroy() {} };
}

// --- plumbing -----------------------------------------------------------------

function repaint(inst: CardInstance): void {
  (inst as Partial<Reactive>).repaint?.();
}

function withCleanup(inst: CardInstance, fn: () => void): CardInstance {
  const d = inst.destroy;
  inst.destroy = () => { d(); fn(); };
  return inst;
}

type Factory = (cfg: Cfg) => CardInstance;
const CARDS: Record<string, Factory> = {
  entities: entitiesCard,
  entity: entityCard,
  glance: glanceCard,
  tile: tileCard,
  button: buttonCard,
  sensor: sensorCard,
  gauge: gaugeCard,
  "history-graph": historyGraphCard,
  "statistics-graph": historyGraphCard,
  markdown: markdownCard,
  "weather-forecast": weatherCard,
  thermostat: thermostatCard,
  light: lightCard,
  "media-control": mediaCard,
  "picture-entity": pictureEntityCard,
  "picture-glance": pictureGlanceCard,
  picture: pictureCard,
  "vertical-stack": (cfg) => container(cfg, "ll-stack column"),
  "horizontal-stack": (cfg) => container(cfg, "ll-stack row"),
  grid: (cfg) => {
    const n = ((cfg.cards || []) as Cfg[]).length;
    return container(cfg, "ll-grid", { gridTemplateColumns: `repeat(${cfg.columns || Math.min(3, n) || 1}, minmax(0, 1fr))` });
  },
  conditional: conditionalCard,
  heading: headingCard,
};

export const SUPPORTED_CARDS = Object.keys(CARDS);

// custom: cards are created by custom.ts, which registers itself here (avoids
// an import cycle: custom cards can nest native cards via loadCardHelpers).
let customFactory: Factory | null = null;
export function registerCustomFactory(f: Factory): void { customFactory = f; }

/** Build any card config — native types here, custom: types via custom.ts. */
export function createCard(cfg: Cfg): CardInstance {
  const type = String(cfg?.type || "");
  try {
    if (type.startsWith("custom:")) return customFactory ? customFactory(cfg) : unsupported(type);
    const f = CARDS[type];
    return f ? f(cfg) : unsupported(type);
  } catch (e) {
    console.error(`[ha] card ${type} failed`, e);
    return { el: h("div", { class: "ll-unsupported" }, `Card error: ${(e as Error).message}`), destroy() {} };
  }
}

// --- more-info sheet (module-level singleton) ---------------------------------

let moreInfoClose: (() => void) | null = null;

export function openMoreInfo(id: string): void {
  moreInfoClose?.();
  const card = h("div", { class: "ll-more-card", onclick: (e: Event) => e.stopPropagation() });
  const root = h("div", { class: "ll-root ll-more" }, card);
  const render = () => {
    const s = st(id);
    const attrs = Object.entries(s?.attributes || {}).filter(([k]) =>
      !["friendly_name", "icon", "entity_picture", "supported_features", "supported_color_modes"].includes(k));
    card.replaceChildren(...[
      h("div", { class: "ll-more-head" },
        icon(entityIcon(s, id), 28, isActive(s) ? "accent" : ""),
        h("div", null, h("strong", null, entityName(s, id)), h("small", { class: "mono dim" }, id)),
        h("button", { type: "button", class: "ll-more-x", "aria-label": "Close", onclick: () => close() }, icon("mdi:close", 18))),
      h("div", { class: "ll-more-state" }, h("span", { class: "ll-big" }, formatState(s)),
        canToggle(id) ? switchEl(isActive(s), () => runAction({ action: "toggle" }, id, "toggle", () => {})) : null),
      h("small", { class: "dim" }, `Changed ${relTime(s?.last_changed)}`),
      attrs.length ? h("dl", { class: "ll-attrs" }, ...attrs.slice(0, 16).map(([k, v]) =>
        h("div", null, h("dt", null, k.replace(/_/g, " ")), h("dd", null, typeof v === "object" ? JSON.stringify(v) : String(v))))) : null,
    ].filter((n): n is HTMLElement => !!n));
  };
  render();
  const unwatch = haHub.watch([id], render);
  const close = () => { unwatch(); root.remove(); moreInfoClose = null; };
  root.addEventListener("click", close);
  document.body.appendChild(root);
  moreInfoClose = close;
}
