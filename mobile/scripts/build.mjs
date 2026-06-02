// Assemble the mobile web bundle (www/):
//   1. sync-web.mjs  -> copy rpi/web display into www/
//   2. esbuild       -> bundle src/boot.ts (+ deps, incl. Capacitor plugins)
//                       into www/assets/shell.js (ESM)
//   3. copy shell index.html + overlay/onboarding CSS into www/

import { cp, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const mobileDir = resolve(here, "..");
const wwwDir = resolve(mobileDir, "www");
const srcDir = resolve(mobileDir, "src");

// 1. Sync the display tree.
execFileSync(process.execPath, [resolve(here, "sync-web.mjs")], { stdio: "inherit" });

// 2. Bundle the shell (boot + overlay + onboarding + platform wrappers).
await mkdir(resolve(wwwDir, "assets"), { recursive: true });
await build({
  entryPoints: [resolve(srcDir, "boot.ts")],
  bundle: true,
  format: "esm",
  target: "es2020",
  outfile: resolve(wwwDir, "assets", "shell.js"),
  sourcemap: true,
  logLevel: "info",
});

// 3. Copy the shell HTML + CSS assets.
await cp(resolve(srcDir, "index.html"), resolve(wwwDir, "index.html"));
for (const css of [
  ["overlay", "overlay.css"],
  ["overlay", "hide-kiosk-controls.css"],
  ["onboarding", "onboarding.css"],
]) {
  await cp(resolve(srcDir, ...css), resolve(wwwDir, "assets", css[1]));
}

console.log("build: www/ assembled");
