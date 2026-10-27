// Material Design Icons for the card layer. The build (scripts/build.mjs)
// extracts every @mdi/js path into www/assets/mdi.json once (keyed by camel
// name, e.g. "MoleculeCo2"); it's a local app
// asset, so icons work identically at home and through the gateway. Loaded
// lazily on the first icon, then served from memory.

let table: Promise<Record<string, string>> | null = null;

function load(): Promise<Record<string, string>> {
  table ||= fetch("assets/mdi.json")
    .then((r) => (r.ok ? r.json() : {}))
    .catch(() => ({}));
  return table;
}

/** "mdi:lightbulb" | "hass:lightbulb" | "lightbulb" → SVG path data, or null. */
export async function mdiPath(icon: string | undefined | null): Promise<string | null> {
  if (!icon) return null;
  // "molecule-co2" → "MoleculeCo2" (the @mdi/js export name minus "mdi").
  const key = icon.replace(/^(mdi|hass):/, "").split("-")
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join("");
  return (await load())[key] ?? null;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** An <svg> element that fills in its path once the table has loaded. */
export function mdiIcon(icon: string, size = 24, className = ""): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", className);
  setMdi(svg, icon);
  return svg;
}

/** Point an existing mdiIcon at a different icon (no re-create, no flicker). */
export function setMdi(svg: SVGSVGElement, icon: string): void {
  if (svg.dataset.icon === icon) return;
  svg.dataset.icon = icon;
  void mdiPath(icon).then((d) => {
    if (svg.dataset.icon !== icon) return; // changed again meanwhile
    svg.replaceChildren();
    if (!d) return;
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "currentColor");
    svg.appendChild(path);
  });
}
