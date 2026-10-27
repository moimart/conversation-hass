// Layout editor for the satellite display. Edits the CURRENT orientation —
// rotate the device to edit the other one. Items (orb, clock, transcript,
// response, HA cards) are boxes on the square-cell grid: drag to move, the
// corner handle to resize, tap for a menu. Changes preview live underneath;
// Done saves to the server, Cancel restores what was there.

import YAML from "yaml";
import { haApi, type LayoutItem, type LovelaceCardRef, type SatelliteLayout } from "../ha/api";
import { createCard, type CardInstance } from "../ha/cards";
import { h } from "../ha/helpers";
import {
  BUILTINS, COLS, currentOrientation, defaultItems, geometry, getLayout, previewLayout, rectOf,
  saveLayout, type Geometry, type Orientation,
} from "./layout";

const LABELS: Record<string, string> = { orb: "Orb", clock: "Clock", transcript: "Transcript", response: "Response" };
const MAX_ROWS = 120;

let active: { close: () => void } | null = null;

function clone<T>(v: T): T { return JSON.parse(JSON.stringify(v)); }

function cardLabel(cfg: Record<string, any>): string {
  const t = String(cfg.type || "card").replace(/^custom:/, "");
  return cfg.title || cfg.name || cfg.entity || t;
}

function overlaps(a: LayoutItem, b: LayoutItem): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** First free spot (top-left scan) for a w×h item, or null if the grid is full. */
function freeSpot(items: LayoutItem[], w: number, hgt: number, cols: number, rows: number): { x: number; y: number } | null {
  for (let y = 0; y + hgt <= Math.max(rows, hgt); y++) {
    for (let x = 0; x + w <= cols; x++) {
      const cand = { id: "_", x, y, w, h: hgt };
      if (!items.some((i) => overlaps(i, cand))) return { x, y };
    }
  }
  return null;
}

export function startLayoutEditor(): void {
  if (active) return;
  const original = clone(getLayout());
  const draft: SatelliteLayout = clone(original);
  let orient: Orientation = currentOrientation();
  let g: Geometry = geometry(orient);

  const ensureItems = () => {
    if (!draft[orient].items.length) draft[orient].items = defaultItems(orient, g);
  };
  ensureItems();
  previewLayout(draft);

  const root = h("div", { class: "hal-layout-editor" });
  const grid = h("div", { class: "hle-grid" });
  const hint = h("div", { class: "hle-hint" });
  const toolbar = h("div", { class: "hle-toolbar" },
    h("button", { type: "button", class: "hle-btn", onclick: () => openAddSheet() }, "＋ Add"),
    h("span", { class: "hle-spacer" }),
    h("button", { type: "button", class: "hle-btn ghost", onclick: () => close(false) }, "Cancel"),
    h("button", { type: "button", class: "hle-btn primary", onclick: () => void done() }, "Done"),
  );
  root.append(grid, hint, toolbar);
  document.body.appendChild(root);
  document.body.classList.add("hal-layout-editing");

  const items = () => draft[orient].items;

  function render(): void {
    g = geometry(orient);
    hint.textContent = `Editing ${orient} — rotate the device to edit ${orient === "portrait" ? "landscape" : "portrait"}`;
    Object.assign(grid.style, {
      left: `${g.left}px`, top: `${g.top}px`, width: `${g.width}px`, height: `${g.rows * g.cell}px`,
      backgroundSize: `${g.cell}px ${g.cell}px`,
    });
    grid.replaceChildren(...items().map(boxFor));
  }

  function boxFor(it: LayoutItem): HTMLElement {
    const r = rectOf(it, g);
    const cfg = draft.cards.find((c) => c.id === it.id)?.config;
    const label = LABELS[it.id] || (cfg ? cardLabel(cfg) : it.id);
    const handle = h("div", { class: "hle-handle", "aria-label": "Resize" });
    const box = h("div", { class: `hle-box ${it.id === "orb" ? "orb" : ""}`, style: {
      left: `${r.x - g.left}px`, top: `${r.y - g.top}px`, width: `${r.w}px`, height: `${r.h}px`,
    } }, h("span", { class: "hle-label" }, label), handle);
    attachDrag(box, handle, it);
    return box;
  }

  /** Drag-to-move and handle-to-resize, snapped to cells, never overlapping. */
  function attachDrag(box: HTMLElement, handle: HTMLElement, it: LayoutItem): void {
    box.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const resizing = ev.target === handle;
      const start = { px: ev.clientX, py: ev.clientY, x: it.x, y: it.y, w: it.w, h: it.h };
      let moved = false;
      box.setPointerCapture(ev.pointerId);
      box.classList.add("dragging");
      const move = (e: PointerEvent) => {
        const dx = Math.round((e.clientX - start.px) / g.cell);
        const dy = Math.round((e.clientY - start.py) / g.cell);
        if (Math.abs(e.clientX - start.px) + Math.abs(e.clientY - start.py) > 6) moved = true;
        const next = { ...it };
        if (resizing) {
          next.w = Math.max(1, Math.min(g.cols - it.x, start.w + dx));
          next.h = Math.max(1, Math.min(MAX_ROWS - it.y, start.h + dy));
          if (it.id === "orb") next.w = next.h = Math.max(1, Math.min(next.w, next.h, g.cols - it.x));
        } else {
          next.x = Math.max(0, Math.min(g.cols - it.w, start.x + dx));
          next.y = Math.max(0, Math.min(Math.max(0, g.rows - it.h), start.y + dy));
        }
        if (items().some((o) => o !== it && overlaps(o, next))) return; // keep last valid
        if (next.x === it.x && next.y === it.y && next.w === it.w && next.h === it.h) return;
        Object.assign(it, next);
        const r = rectOf(it, g);
        Object.assign(box.style, { left: `${r.x - g.left}px`, top: `${r.y - g.top}px`, width: `${r.w}px`, height: `${r.h}px` });
        previewLayout(draft);
      };
      const up = () => {
        box.removeEventListener("pointermove", move);
        box.removeEventListener("pointerup", up);
        box.removeEventListener("pointercancel", up);
        box.classList.remove("dragging");
        if (!moved && !resizing) openItemMenu(it);
      };
      box.addEventListener("pointermove", move);
      box.addEventListener("pointerup", up);
      box.addEventListener("pointercancel", up);
    });
  }

  // --- menus -----------------------------------------------------------------------

  function sheet(titleText: string, body: HTMLElement): { el: HTMLElement; close: () => void } {
    const panel = h("div", { class: "hle-sheet", onclick: (e: Event) => e.stopPropagation() },
      h("div", { class: "hle-sheet-title" }, titleText), body);
    const back = h("div", { class: "hle-sheet-backdrop" }, panel);
    const closeSheet = () => back.remove();
    back.addEventListener("click", closeSheet);
    document.body.appendChild(back);
    return { el: panel, close: closeSheet };
  }

  function openItemMenu(it: LayoutItem): void {
    const card = draft.cards.find((c) => c.id === it.id);
    const list = h("div", { class: "hle-menu" });
    const s = sheet(LABELS[it.id] || (card ? cardLabel(card.config) : it.id), list);
    const opt = (text: string, fn: () => void, danger = false) =>
      list.append(h("button", { type: "button", class: `hle-menu-btn ${danger ? "danger" : ""}`, onclick: () => { s.close(); fn(); } }, text));
    if (card) opt("Edit card", () => openYamlSheet(card.config, (cfg) => { card.config = cfg; refresh(); }));
    if (it.id !== "orb") {
      opt(`Remove from ${orient}`, () => {
        draft[orient].items = items().filter((i) => i !== it);
        refresh();
      });
    }
    if (card) {
      opt("Delete card everywhere", () => {
        draft.cards = draft.cards.filter((c) => c !== card);
        for (const o of ["portrait", "landscape"] as Orientation[]) {
          draft[o].items = draft[o].items.filter((i) => i.id !== card.id);
        }
        refresh();
      }, true);
    }
  }

  function placeItem(id: string, w: number, hgt: number): boolean {
    const ww = Math.min(w, g.cols);
    const spot = freeSpot(items(), ww, hgt, g.cols, Math.max(g.rows, 1));
    if (!spot) { alert("No free space on the grid — shrink or remove something first."); return false; }
    items().push({ id, x: spot.x, y: spot.y, w: ww, h: hgt });
    return true;
  }

  function addCard(cfg: Record<string, any>): void {
    let n = draft.cards.length + 1;
    while (draft.cards.some((c) => c.id === `c${n}`)) n++;
    const id = `c${n}`;
    draft.cards.push({ id, config: cfg });
    const w = orient === "portrait" ? 6 : 8;
    if (!placeItem(id, w, 4)) draft.cards.pop();
    refresh();
  }

  function openAddSheet(): void {
    const tabs = h("div", { class: "hle-tabs" });
    const pane = h("div", { class: "hle-pane" });
    const s = sheet("Add to this layout", h("div", null, tabs, pane));
    const placed = new Set(items().map((i) => i.id));

    const showExisting = () => {
      const hidden = BUILTINS.filter((b) => !placed.has(b));
      const other = draft.cards.filter((c) => !placed.has(c.id));
      pane.replaceChildren(...(hidden.length || other.length ? [
        ...hidden.map((b) => h("button", { type: "button", class: "hle-menu-btn", onclick: () => {
          s.close();
          const w = b === "orb" ? 6 : b === "clock" ? 8 : 12;
          const hh = b === "orb" ? 6 : b === "clock" ? 2 : 4;
          if (placeItem(b, w, hh)) refresh();
        } }, LABELS[b])),
        ...other.map((c) => h("button", { type: "button", class: "hle-menu-btn", onclick: () => {
          s.close();
          if (placeItem(c.id, orient === "portrait" ? 6 : 8, 4)) refresh();
        } }, `${cardLabel(c.config)} (from ${orient === "portrait" ? "landscape" : "portrait"})`)),
      ] : [h("div", { class: "hle-empty" }, "Everything is already on this layout.")]));
    };

    const showDashboard = async () => {
      pane.replaceChildren(h("div", { class: "hle-empty" }, "Loading dashboards…"));
      try {
        const dashes = await haApi.dashboards();
        const sel = h("select", { class: "hle-select" }, ...dashes.map((d) => h("option", { value: d.url_path }, d.title || d.url_path)));
        const list = h("div", { class: "hle-list" });
        const load = async () => {
          list.replaceChildren(h("div", { class: "hle-empty" }, "Loading cards…"));
          try {
            const cards: LovelaceCardRef[] = await haApi.cards(sel.value);
            list.replaceChildren(...(cards.length ? cards.map((c) => h("button", { type: "button", class: "hle-card-ref",
              onclick: () => { s.close(); addCard(c.card); } },
              h("span", { class: "hle-badge" }, c.type.replace(/^custom:/, "✦ ")),
              h("span", { class: "hle-ref-title" }, c.title || "(untitled)"),
              h("small", null, c.path))) : [h("div", { class: "hle-empty" }, "No cards on this dashboard.")]));
          } catch (e) { list.replaceChildren(h("div", { class: "hle-empty" }, `Couldn't load cards: ${(e as Error).message}`)); }
        };
        sel.addEventListener("change", () => void load());
        pane.replaceChildren(sel, list);
        void load();
      } catch (e) {
        pane.replaceChildren(h("div", { class: "hle-empty" }, `Home Assistant unavailable: ${(e as Error).message}`));
      }
    };

    const showEntity = () => {
      const input = h("input", { class: "hle-input", type: "search", placeholder: "Search entities…" });
      const list = h("div", { class: "hle-list" });
      let t: number | undefined;
      const search = async () => {
        try {
          const ents = await haApi.entities(input.value.trim());
          list.replaceChildren(...ents.map((e) => h("button", { type: "button", class: "hle-card-ref",
            onclick: () => { s.close(); addCard({ type: "tile", entity: e.id }); } },
            h("span", { class: "hle-ref-title" }, e.name), h("small", null, `${e.id} · ${e.state}${e.unit ? ` ${e.unit}` : ""}`))));
        } catch (err) { list.replaceChildren(h("div", { class: "hle-empty" }, (err as Error).message)); }
      };
      input.addEventListener("input", () => { clearTimeout(t); t = window.setTimeout(() => void search(), 150); });
      pane.replaceChildren(input, list);
      void search();
      input.focus();
    };

    const showYaml = () => { s.close(); openYamlSheet({ type: "tile", entity: "" }, addCard); };

    const tabDefs: [string, () => void][] = [
      ["Dashboard", () => void showDashboard()], ["Entity", showEntity], ["YAML", showYaml], ["Hidden", showExisting],
    ];
    for (const [label, fn] of tabDefs) {
      tabs.append(h("button", { type: "button", class: "hle-tab", onclick: (e: Event) => {
        tabs.querySelectorAll(".hle-tab").forEach((b) => b.classList.remove("on"));
        (e.currentTarget as HTMLElement).classList.add("on");
        fn();
      } }, label));
    }
    (tabs.firstElementChild as HTMLElement).classList.add("on");
    void showDashboard();
  }

  function openYamlSheet(initial: Record<string, any>, onSave: (cfg: Record<string, any>) => void): void {
    const ta = h("textarea", { class: "hle-yaml", spellcheck: "false", autocapitalize: "off" });
    ta.value = YAML.stringify(initial);
    const err = h("div", { class: "hle-error" });
    const preview = h("div", { class: "hle-preview ll-root" });
    let inst: CardInstance | null = null;
    let parsed: Record<string, any> | null = null;
    const update = () => {
      try {
        const v = YAML.parse(ta.value);
        if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("A card is a mapping with at least a `type:`");
        if (!v.type) throw new Error("Missing `type:` (e.g. tile, entities, gauge…)");
        parsed = v;
        err.textContent = "";
        inst?.destroy();
        inst = createCard(v);
        preview.replaceChildren(inst.el);
      } catch (e) {
        parsed = null;
        err.textContent = (e as Error).message.split("\n")[0];
      }
    };
    let t: number | undefined;
    ta.addEventListener("input", () => { clearTimeout(t); t = window.setTimeout(update, 300); });
    const save = h("button", { type: "button", class: "hle-btn primary", onclick: () => {
      if (!parsed) return;
      inst?.destroy();
      s.close();
      onSave(parsed);
    } }, "Save card");
    const s = sheet("Card", h("div", { class: "hle-yaml-wrap" }, ta, err, preview, save));
    const origClose = s.close;
    s.close = () => { inst?.destroy(); origClose(); };
    update();
  }

  // --- lifecycle --------------------------------------------------------------------

  function refresh(): void {
    previewLayout(draft);
    render();
  }

  const onRotate = () => {
    window.setTimeout(() => {
      const now = currentOrientation();
      if (now !== orient) { orient = now; g = geometry(orient); ensureItems(); previewLayout(draft); }
      render();
    }, 120);
  };
  window.addEventListener("resize", onRotate);

  async function done(): Promise<void> {
    try {
      await saveLayout(draft);
      close(true);
    } catch (e) {
      alert(`Couldn't save the layout: ${(e as Error).message}`);
    }
  }

  function close(saved: boolean): void {
    window.removeEventListener("resize", onRotate);
    root.remove();
    document.querySelectorAll(".hle-sheet-backdrop").forEach((b) => b.remove());
    document.body.classList.remove("hal-layout-editing");
    if (!saved) previewLayout(original);
    active = null;
  }

  active = { close: () => close(false) };
  render();
}

export { COLS };
