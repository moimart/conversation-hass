// Satellite display layout: places HA cards and moves the orb, clock,
// transcript and response, with separate portrait and landscape layouts
// stored per device on the server (server/app/layouts.py).
//
// With NO saved layout nothing changes — hide-kiosk-controls.css keeps the
// stock phone look. Once a layout exists, `body.hal-custom-layout` is set and
// this module positions everything on a square-cell grid:
//   portrait  COLS.portrait  (12) columns,  landscape COLS.landscape (24)
// spanning the screen between the top gear row and the input bar. Cells are
// square, so a layout scales with the device instead of being pixel-bound.
//
// The orb (.eye-container) is never moved in the DOM (app.js looks it up by
// class all over) and can only be SCALED (fixed px internals), so it gets
// inline position + scale. Inline !important beats the stylesheet's !important.

import { haBootstrap, layoutApi, type LayoutItem, type SatelliteLayout } from "../ha/api";
import { createCard, repaintAllCards, type CardInstance } from "../ha/cards";
import { setRegistryIcons } from "../ha/helpers";
import "../ha/custom"; // registers the custom: card factory
import { haHub } from "../ha/hub";

export type Orientation = "portrait" | "landscape";
export const COLS: Record<Orientation, number> = { portrait: 12, landscape: 24 };
/** Grid resolution. 1 = the first release's 6/12 columns; layouts saved at a
 *  lower resolution are scaled up on load (normalize). */
export const GRID_RES = 2;
export const BUILTINS = ["orb", "clock", "transcript", "response"] as const;
const ORB_PX = 406; // .eye-container native size (rpi/web/style.css)
const TOP_RESERVE = 52; // gear row
const BOTTOM_RESERVE = 78; // input bar

export interface Geometry {
  left: number; top: number; cell: number; cols: number; rows: number; width: number; height: number;
}

let layout: SatelliteLayout | null = null;
let mounted = new Map<string, { inst: CardInstance; box: HTMLElement; fit: ResizeObserver }>();
let cardsRoot: HTMLElement | null = null;
const changeListeners = new Set<() => void>();

export function currentOrientation(): Orientation {
  return window.matchMedia("(orientation: portrait)").matches ? "portrait" : "landscape";
}

function safeInsets(): { top: number; right: number; bottom: number; left: number } {
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;visibility:hidden;pointer-events:none;" +
    "padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)";
  document.body.appendChild(probe);
  const cs = getComputedStyle(probe);
  const out = { top: parseFloat(cs.paddingTop) || 0, right: parseFloat(cs.paddingRight) || 0,
    bottom: parseFloat(cs.paddingBottom) || 0, left: parseFloat(cs.paddingLeft) || 0 };
  probe.remove();
  const inset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--hal-inset-top")) || 0;
  out.top = Math.max(out.top, inset);
  return out;
}

export function geometry(orient: Orientation = currentOrientation()): Geometry {
  const s = safeInsets();
  const cols = COLS[orient];
  const left = s.left + 8;
  const width = window.innerWidth - left - s.right - 8;
  const top = s.top + TOP_RESERVE;
  const height = window.innerHeight - top - s.bottom - BOTTOM_RESERVE;
  const cell = width / cols;
  return { left, top, cell, cols, rows: Math.max(1, Math.floor(height / cell)), width, height };
}

export function rectOf(it: LayoutItem, g: Geometry): { x: number; y: number; w: number; h: number } {
  return { x: g.left + it.x * g.cell, y: g.top + it.y * g.cell, w: it.w * g.cell, h: it.h * g.cell };
}

/** A sensible starting layout for an orientation (used by the editor when the
 *  device has none yet): clock top-left, transcript, orb, response. */
export function defaultItems(orient: Orientation, g: Geometry = geometry(orient)): LayoutItem[] {
  const R = g.rows;
  if (orient === "portrait") {
    const orb = Math.max(4, Math.min(12, R - 10));
    return [
      { id: "clock", x: 0, y: 0, w: 8, h: 2 },
      { id: "transcript", x: 0, y: 2, w: 12, h: 4 },
      { id: "orb", x: Math.floor((12 - orb) / 2), y: 6, w: orb, h: orb },
      { id: "response", x: 0, y: Math.min(6 + orb, R - 4), w: 12, h: 4 },
    ];
  }
  const orb = Math.max(4, Math.min(12, R));
  const half = Math.max(2, Math.floor((R - 2) / 2));
  return [
    { id: "orb", x: 0, y: 0, w: orb, h: orb },
    { id: "clock", x: orb, y: 0, w: 8, h: 2 },
    { id: "transcript", x: orb, y: 2, w: 24 - orb, h: half },
    { id: "response", x: orb, y: 2 + half, w: 24 - orb, h: Math.max(2, R - 2 - half) },
  ];
}

const overlapsItem = (a: LayoutItem, b: LayoutItem) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** Bring a layout from the server up to the current grid resolution. Layouts
 *  saved by the first release (6/12 columns) are scaled ×2, and get their clock
 *  back: that release's server dropped the clock's placement on save, which
 *  hid the clock + weather on every edited layout. */
export function normalize(l: SatelliteLayout): SatelliteLayout {
  const res = l.res ?? 1;
  if (res >= GRID_RES) return l;
  const k = GRID_RES / res;
  const out: SatelliteLayout = { ...l, res: GRID_RES };
  for (const o of ["portrait", "landscape"] as Orientation[]) {
    const items = (l[o]?.items ?? []).map((i) => ({ id: i.id, x: i.x * k, y: i.y * k, w: i.w * k, h: i.h * k }));
    if (items.length && !items.some((i) => i.id === "clock")) {
      const clock = { id: "clock", x: 0, y: 0, w: 8, h: 2 };
      if (!items.some((i) => overlapsItem(i, clock))) items.push(clock);
      else {
        // Top-left is taken: put it in the first free 8×2 slot, if any.
        for (let y = 0; y < 120 && !items.some((i) => i.id === "clock"); y++) {
          for (let x = 0; x + 8 <= COLS[o]; x++) {
            const c = { id: "clock", x, y, w: 8, h: 2 };
            if (!items.some((i) => overlapsItem(i, c))) { items.push(c); break; }
          }
        }
      }
    }
    out[o] = { items };
  }
  return out;
}

// --- applying ------------------------------------------------------------------

const IMP = "important";
function setStyles(el: HTMLElement | null, styles: Record<string, string>): void {
  if (!el) return;
  for (const [k, v] of Object.entries(styles)) el.style.setProperty(k, v, IMP);
}
function clearStyles(el: HTMLElement | null, keys: string[]): void {
  if (!el) return;
  for (const k of keys) el.style.removeProperty(k);
}

const BOX_KEYS = ["left", "top", "right", "bottom", "width", "height", "max-height", "max-width", "transform", "transform-origin", "display"];

function placeBuiltin(id: string, it: LayoutItem | undefined, g: Geometry): void {
  const sel: Record<string, string> = {
    orb: "#display-root .eye-container",
    clock: "#display-root .clock-display",
    transcript: "#display-root .transcript-container",
    response: "#display-root .response-container",
  };
  const el = document.querySelector<HTMLElement>(sel[id]);
  if (!el) return;
  clearStyles(el, BOX_KEYS);
  if (!it) {
    // Not placed in this orientation: hidden (the orb is always placed).
    if (id !== "orb") setStyles(el, { display: "none" });
    return;
  }
  const r = rectOf(it, g);
  if (id === "orb") {
    const size = Math.min(r.w, r.h);
    setStyles(el, {
      left: `${r.x + r.w / 2}px`, top: `${r.y + r.h / 2}px`,
      transform: `translate(-50%, -50%) scale(${(size / ORB_PX).toFixed(4)})`,
    });
  } else if (id === "clock") {
    setStyles(el, { left: `${r.x}px`, top: `${r.y}px`, "max-width": `${r.w}px` });
  } else {
    setStyles(el, {
      left: `${r.x}px`, top: `${r.y}px`, right: "auto", bottom: "auto",
      width: `${r.w}px`, height: `${r.h}px`, "max-height": `${r.h}px`, "max-width": "none",
    });
  }
}

function ensureCardsRoot(): HTMLElement | null {
  if (cardsRoot?.isConnected) return cardsRoot;
  const wrapper = document.getElementById("orientation-wrapper");
  if (!wrapper) return null;
  cardsRoot = document.createElement("div");
  cardsRoot.id = "hal-cards-root";
  cardsRoot.className = "ll-root";
  wrapper.appendChild(cardsRoot);
  return cardsRoot;
}

/** Shrink a card to fit its box instead of overflowing (docker-home FitBox):
 *  text cards reflow wider down to 60 %, custom cards scale uniformly to 30 %. */
function fitBox(box: HTMLElement, inner: HTMLElement, uniform: boolean): ResizeObserver {
  const MIN = uniform ? 0.3 : 0.6;
  let scale = 1;
  const fit = () => {
    const natural = inner.offsetHeight;
    if (!natural) return;
    const next = Math.round(Math.max(MIN, Math.min(1, box.clientHeight / natural)) * 50) / 50;
    if (Math.abs(scale - next) < 0.03) return;
    scale = next;
    if (scale >= 1) { inner.style.removeProperty("transform"); inner.style.removeProperty("width"); return; }
    inner.style.transform = `scale(${scale})`;
    if (uniform) { inner.style.transformOrigin = "top center"; inner.style.removeProperty("width"); }
    else { inner.style.transformOrigin = "top left"; inner.style.width = `${100 / scale}%`; }
  };
  const ro = new ResizeObserver(fit);
  ro.observe(box);
  ro.observe(inner);
  return ro;
}

function mountCard(id: string, config: Record<string, any>): { inst: CardInstance; box: HTMLElement; fit: ResizeObserver } {
  const inst = createCard(config);
  const inner = document.createElement("div");
  inner.className = "hal-card-inner";
  inner.appendChild(inst.el);
  const box = document.createElement("div");
  box.className = "hal-card";
  box.dataset.cardId = id;
  box.appendChild(inner);
  return { inst, box, fit: fitBox(box, inner, JSON.stringify(config).includes('"custom:')) };
}

function unmountCard(id: string): void {
  const m = mounted.get(id);
  if (!m) return;
  m.fit.disconnect();
  m.inst.destroy();
  m.box.remove();
  mounted.delete(id);
}

/** (Re)apply the layout for the current orientation. Idempotent. */
export function applyLayout(): void {
  const has = !!layout && hasPlacements(layout);
  document.body.classList.toggle("hal-custom-layout", has);
  if (!has) {
    for (const id of [...mounted.keys()]) unmountCard(id);
    for (const id of BUILTINS) placeBuiltinReset(id);
    return;
  }
  const orient = currentOrientation();
  const g = geometry(orient);
  let items = layout![orient].items;
  // An orientation never edited yet falls back to the defaults (cards hidden).
  if (!items.length) items = defaultItems(orient, g);
  const byId = new Map(items.map((i) => [i.id, i]));
  const defaultOrb = defaultItems(orient, g).find((i) => i.id === "orb");
  for (const id of BUILTINS) placeBuiltin(id, byId.get(id) ?? (id === "orb" ? defaultOrb : undefined), g);

  const root = ensureCardsRoot();
  if (!root) return;
  const configs = new Map(layout!.cards.map((c) => [c.id, c.config]));
  // Unmount cards not placed here (or deleted / config changed).
  for (const [id, m] of mounted) {
    const cfg = configs.get(id);
    if (!byId.has(id) || !cfg || (m.box.dataset.cfg !== JSON.stringify(cfg))) unmountCard(id);
  }
  for (const [id, cfg] of configs) {
    const it = byId.get(id);
    if (!it) continue;
    let m = mounted.get(id);
    if (!m) {
      m = mountCard(id, cfg);
      m.box.dataset.cfg = JSON.stringify(cfg);
      root.appendChild(m.box);
      mounted.set(id, m);
    }
    const r = rectOf(it, g);
    // 3 px gutter so neighbouring cards don't touch.
    Object.assign(m.box.style, { left: `${r.x + 3}px`, top: `${r.y + 3}px`, width: `${r.w - 6}px`, height: `${r.h - 6}px` });
  }
  changeListeners.forEach((fn) => fn());
}

function placeBuiltinReset(id: string): void {
  const sel: Record<string, string> = {
    orb: "#display-root .eye-container", clock: "#display-root .clock-display",
    transcript: "#display-root .transcript-container", response: "#display-root .response-container",
  };
  clearStyles(document.querySelector<HTMLElement>(sel[id]), BOX_KEYS);
}

function hasPlacements(l: SatelliteLayout): boolean {
  return l.cards.length > 0 || l.portrait.items.length > 0 || l.landscape.items.length > 0;
}

// --- state for the editor ---------------------------------------------------------

export function getLayout(): SatelliteLayout {
  return layout ?? { cards: [], portrait: { items: [] }, landscape: { items: [] }, version: 0, res: GRID_RES };
}

/** Preview an unsaved layout (the editor calls this on every drag). */
export function previewLayout(next: SatelliteLayout): void {
  layout = next;
  applyLayout();
}

export async function saveLayout(next: SatelliteLayout): Promise<SatelliteLayout> {
  const { version: _v, ...body } = next;
  layout = normalize(await layoutApi.put({ ...body, res: GRID_RES }));
  applyLayout();
  return layout;
}

export async function resetLayout(): Promise<void> {
  await layoutApi.reset();
  layout = null;
  applyLayout();
}

export function onLayoutApplied(fn: () => void): () => void {
  changeListeners.add(fn);
  return () => changeListeners.delete(fn);
}

// --- boot ----------------------------------------------------------------------

/** Mount the satellite layout: attach the HA hub to the display socket, fetch
 *  this device's layout and apply it; re-apply on rotation/resize. */
export async function mountLayout(): Promise<void> {
  haHub.attach();
  // Entity-registry icons (set in HA's entity settings) for the native cards.
  const loadIcons = (attempt = 0): void => {
    haBootstrap()
      .then((b) => { setRegistryIcons(b.registry?.entities, repaintAllCards); repaintAllCards(); })
      .catch(() => { if (attempt < 6) setTimeout(() => loadIcons(attempt + 1), 5000 * (attempt + 1)); });
  };
  loadIcons();
  let t: number | undefined;
  const relayout = () => { clearTimeout(t); t = window.setTimeout(applyLayout, 80); };
  window.addEventListener("resize", relayout);
  window.matchMedia("(orientation: portrait)").addEventListener("change", relayout);
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      layout = normalize(await layoutApi.get());
      break;
    } catch (e) {
      // Old server (no route) or offline: keep the stock look, retry briefly.
      console.warn("[layout] fetch failed", e);
      await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
    }
  }
  applyLayout();
}
