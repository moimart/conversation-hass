// Copy the kiosk display (rpi/web) into the mobile web bundle (www/).
//
// rpi/web is the SINGLE source of the display UI — we never edit the copies, so
// there is no fork and no drift (www/ is regenerated every build). The display
// scripts (app.js, photo_frame.js, calendar.js, state_videos.js) read an
// injected window.HAL_CONFIG to target the AI server (see rpi/web changes).
//
// The kiosk's index.html is copied to display.html (not index.html) because the
// mobile shell provides its own index.html; boot.ts injects display.html's body
// then loads app.js once HAL_CONFIG is ready.

import { cp, rm, mkdir, rename, access } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const mobileDir = resolve(here, "..");
const repoRoot = resolve(mobileDir, "..");
const src = resolve(repoRoot, "rpi", "web");
const wwwDir = resolve(mobileDir, "www");

async function exists(p) {
  try { await access(p); return true; } catch { return false; }
}

if (!(await exists(src))) {
  console.error(`sync-web: source not found: ${src}`);
  process.exit(1);
}

// Wipe and recopy the whole display tree (themes are fetched at runtime from
// the server, so we don't copy any themes/ dir — there is none in rpi/web).
await rm(wwwDir, { recursive: true, force: true });
await mkdir(wwwDir, { recursive: true });
await cp(src, wwwDir, { recursive: true });

// index.html -> display.html (shell provides the real index.html).
await rename(resolve(wwwDir, "index.html"), resolve(wwwDir, "display.html"));

console.log(`sync-web: copied ${src} -> ${wwwDir} (index.html -> display.html)`);
