// Custom orbs in the satellite settings sheet (server/app/orbs.py).
//
// The "Orb" section lets this device follow the hub's orb or pick its own,
// choose the HUB's orb (global — the same select HA exposes), create orbs from
// a video or image (plus optional per-state media) and manage them. The
// display side lives in rpi/web/app.js (window.HALOrbLocal + set_orb).

import type { HalConfig } from "../config/hal-config";

interface OrbInfo { id: string; name: string; states: string[] }
interface OrbLocalApi {
  get(): { follow: boolean; id: string; hub: string };
  set(p: { follow?: boolean; id?: string }): Promise<void>;
  reload(): Promise<void>;
}

const STATES: [string, string][] = [
  ["idle", "Idle (required)"],
  ["listening", "Listening"],
  ["processing", "Thinking"],
  ["speaking", "Speaking"],
];

export const ORB_SECTION_HTML = `
      <div class="hal-sheet-title" style="margin-top:14px">Orb</div>
      <div class="hal-sheet-row">
        <div class="hal-sheet-row-text">
          <div class="hal-sheet-row-label">Hub orb</div>
          <div class="hal-sheet-row-sub">Shown on the hub and every device that follows it.</div>
        </div>
        <select class="hal-sheet-select" id="hal-orb-hub"></select>
      </div>
      <div class="hal-sheet-row">
        <div class="hal-sheet-row-text">
          <div class="hal-sheet-row-label">Follow hub orb</div>
          <div class="hal-sheet-row-sub" id="hal-orb-sub">This device shows the hub's orb.</div>
        </div>
        <button class="hal-switch on" id="hal-orb-follow" role="switch" aria-checked="true" aria-label="Follow hub orb"></button>
      </div>
      <div class="hal-sheet-row" id="hal-orb-local-row" hidden>
        <div class="hal-sheet-row-text"><div class="hal-sheet-row-label">This device's orb</div></div>
        <select class="hal-sheet-select" id="hal-orb-local"></select>
      </div>
      <div class="hal-sheet-namerow">
        <button class="hal-sheet-btn" id="hal-orb-new">New orb…</button>
        <button class="hal-sheet-btn" id="hal-orb-manage">Manage…</button>
      </div>`;

function api(cfg: HalConfig) {
  const base = cfg.serverBaseUrl.replace(/\/+$/, "");
  const auth: Record<string, string> = cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {};
  const json = async (r: Response) => {
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `${r.status}`);
    return body;
  };
  const send = (method: string, path: string, body?: unknown) =>
    fetch(`${base}${path}`, {
      method, headers: { ...auth, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }).then(json);
  return {
    list: () => send("GET", "/api/orbs") as Promise<{ orbs: OrbInfo[]; selected: string }>,
    create: (name: string) => send("POST", "/api/orbs", { name }) as Promise<OrbInfo>,
    rename: (id: string, name: string) => send("PATCH", `/api/orbs/${id}`, { name }),
    remove: (id: string) => send("DELETE", `/api/orbs/${id}`),
    removeMedia: (id: string, st: string) => send("DELETE", `/api/orbs/${id}/media/${st}`),
    select: (id: string) => send("POST", "/api/orbs/select", { id }),
    upload: (id: string, st: string, file: File) =>
      fetch(`${base}/api/orbs/${id}/media/${st}`, {
        method: "PUT", headers: { ...auth, "Content-Type": file.type || "application/octet-stream" }, body: file,
      }).then(json) as Promise<OrbInfo>,
  };
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, props: Record<string, any> = {}, ...kids: (Node | string)[]) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else (e as any)[k] = v;
  }
  e.append(...kids);
  return e;
}

function sheet(title: string, body: HTMLElement): () => void {
  const panel = el("div", { class: "hle-sheet", onclick: (e: Event) => e.stopPropagation() },
    el("div", { class: "hle-sheet-title" }, title), body);
  const back = el("div", { class: "hle-sheet-backdrop hal-orb-sheet" }, panel);
  // The settings sheet sits at 2147483200 — stack above it.
  back.style.zIndex = "2147483300";
  const close = () => back.remove();
  back.addEventListener("click", close);
  document.body.appendChild(back);
  return close;
}

export function mountOrbSection(cfg: HalConfig, back: HTMLElement): void {
  const local = (window as unknown as { HALOrbLocal?: OrbLocalApi }).HALOrbLocal;
  const hubSel = back.querySelector<HTMLSelectElement>("#hal-orb-hub");
  const followSw = back.querySelector<HTMLButtonElement>("#hal-orb-follow");
  const localRow = back.querySelector<HTMLElement>("#hal-orb-local-row");
  const localSel = back.querySelector<HTMLSelectElement>("#hal-orb-local");
  const sub = back.querySelector<HTMLElement>("#hal-orb-sub");
  const newBtn = back.querySelector<HTMLButtonElement>("#hal-orb-new");
  const manageBtn = back.querySelector<HTMLButtonElement>("#hal-orb-manage");
  if (!local || !hubSel || !followSw || !localRow || !localSel || !newBtn || !manageBtn) return;
  const srv = api(cfg);
  let orbs: OrbInfo[] = [];

  const fill = (sel: HTMLSelectElement, current: string) => {
    sel.replaceChildren(el("option", { value: "default", textContent: "Default (theme)" }),
      ...orbs.filter((o) => o.states.includes("idle"))
        .map((o) => el("option", { value: o.id, textContent: o.name })));
    sel.value = current;
    if (sel.value !== current) sel.value = "default";
  };
  const render = (hubSelected: string) => {
    const p = local.get();
    followSw.classList.toggle("on", p.follow);
    followSw.setAttribute("aria-checked", String(p.follow));
    localRow.hidden = p.follow;
    if (sub) sub.textContent = p.follow ? "This device shows the hub's orb." : "This device shows its own orb.";
    fill(hubSel, hubSelected);
    fill(localSel, p.id);
  };
  const reload = async () => {
    try {
      const r = await srv.list();
      orbs = r.orbs;
      render(r.selected);
    } catch (e) {
      hubSel.replaceChildren(el("option", { textContent: "unavailable" }));
      hubSel.disabled = true;
    }
  };
  void reload();

  hubSel.addEventListener("change", () => {
    srv.select(hubSel.value).catch((e) => alert(`Couldn't change the hub orb: ${(e as Error).message}`));
  });
  followSw.addEventListener("click", async () => {
    const p = local.get();
    await local.set({ follow: !p.follow, id: p.id });
    render(hubSel.value);
  });
  localSel.addEventListener("change", () => void local.set({ follow: false, id: localSel.value }));
  newBtn.addEventListener("click", () => openEditor(cfg, null, async () => { await reload(); await local.reload(); }));
  manageBtn.addEventListener("click", () => openManager(cfg, orbs, async () => { await reload(); await local.reload(); }));
}

function openManager(cfg: HalConfig, orbs: OrbInfo[], changed: () => Promise<void>): void {
  const srv = api(cfg);
  const list = el("div", { class: "hle-menu" });
  const close = sheet("Orbs", list);
  if (!orbs.length) list.append(el("div", { class: "hle-empty" }, "No custom orbs yet — create one with “New orb…”."));
  for (const o of orbs) {
    const states = STATES.filter(([k]) => o.states.includes(k)).map(([, l]) => l.replace(" (required)", "")).join(", ");
    list.append(el("div", { class: "hle-card-ref" },
      el("span", { class: "hle-ref-title" }, o.name),
      el("small", {}, states || "no media yet"),
      el("div", { class: "hal-orb-actions" },
        el("button", { class: "hle-btn", onclick: () => { close(); openEditor(cfg, o, changed); } }, "Edit"),
        el("button", { class: "hle-btn ghost", onclick: async () => {
          const name = prompt("Rename orb", o.name);
          if (!name || name === o.name) return;
          try { await srv.rename(o.id, name); close(); await changed(); }
          catch (e) { alert((e as Error).message); }
        } }, "Rename"),
        el("button", { class: "hle-btn ghost danger", onclick: async () => {
          if (!confirm(`Delete “${o.name}”? Devices showing it go back to the default orb.`)) return;
          try { await srv.remove(o.id); close(); await changed(); }
          catch (e) { alert((e as Error).message); }
        } }, "Delete"))));
  }
}

/** Create a new orb (orb = null) or edit an existing one's media. */
function openEditor(cfg: HalConfig, orb: OrbInfo | null, changed: () => Promise<void>): void {
  const srv = api(cfg);
  const name = el("input", { class: "hle-input", type: "text", maxLength: 40, placeholder: "Name (e.g. Cosmic cat)",
    value: orb?.name ?? "", disabled: !!orb });
  const picks = new Map<string, File>();
  const status = el("div", { class: "hle-error" });
  const rows = STATES.map(([st, label]) => {
    const has = !!orb?.states.includes(st);
    const info = el("small", {}, has ? "set — choose to replace" : st === "idle" ? "required" : "optional — falls back to idle");
    const input = el("input", { type: "file", accept: "video/*,image/*", hidden: true });
    input.addEventListener("change", () => {
      const f = input.files?.[0];
      if (!f) return;
      picks.set(st, f);
      info.textContent = `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`;
    });
    const remove = has && st !== "idle"
      ? el("button", { class: "hle-btn ghost", onclick: async () => {
          try { await srv.removeMedia(orb!.id, st); info.textContent = "removed"; await changed(); }
          catch (e) { status.textContent = (e as Error).message; }
        } }, "Remove")
      : "";
    return el("div", { class: "hal-orb-row" },
      el("div", { class: "hal-orb-row-text" }, el("span", {}, label), info),
      remove,
      el("button", { class: "hle-btn", onclick: () => input.click() }, "Choose…"),
      input);
  });
  const save = el("button", { class: "hle-btn primary" }, orb ? "Save" : "Create");
  const close = sheet(orb ? `Edit “${orb.name}”` : "New orb",
    el("div", { class: "hle-yaml-wrap" }, name, ...rows, status, save));
  save.addEventListener("click", async () => {
    if (!orb && !name.value.trim()) { status.textContent = "Give the orb a name."; return; }
    if (!orb && !picks.has("idle")) { status.textContent = "Choose the idle video or image."; return; }
    save.disabled = true;
    let created: OrbInfo | null = null;
    try {
      const target = orb ?? (created = await srv.create(name.value.trim()));
      let i = 0;
      for (const [st] of STATES) {
        const f = picks.get(st);
        if (!f) continue;
        status.textContent = `Uploading and processing ${++i} of ${picks.size}…`;
        await srv.upload(target.id, st, f);
      }
      close();
      await changed();
    } catch (e) {
      status.textContent = (e as Error).message;
      save.disabled = false;
      // A brand-new orb whose idle upload failed would linger media-less.
      if (created) await srv.remove(created.id).catch(() => {});
    }
  });
}
