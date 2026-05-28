// Media inlining for outbound delivery to the HAL server.
//
// Agent-generated media (images, audio) often arrives as a path on the
// gateway host's filesystem (e.g. ~/.openclaw/media/...). The Dockerized
// HAL server can't reach those paths, so we read the file here (in the
// gateway/Node process) and hand it to HAL as a base64 data: URL. http(s)
// and existing data: URLs are passed through untouched.

import { readFile } from "node:fs/promises";
import { extname } from "node:path";
import { fileURLToPath } from "node:url";

const MAX_INLINE_BYTES = 20 * 1024 * 1024; // 20 MB

const MIME_BY_EXT: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mov": "video/quicktime",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
  ".oga": "audio/ogg",
  ".opus": "audio/opus",
  ".m4a": "audio/mp4",
  ".aac": "audio/aac",
  ".flac": "audio/flac",
};

export async function inlineLocalMedia(url: string): Promise<string | null> {
  if (!url) return null;
  if (
    url.startsWith("http://") ||
    url.startsWith("https://") ||
    url.startsWith("data:")
  ) {
    return url;
  }

  let path = url;
  if (path.startsWith("file://")) {
    try {
      path = fileURLToPath(path);
    } catch {
      path = path.slice("file://".length);
    }
  }

  try {
    const buf = await readFile(path);
    if (buf.length > MAX_INLINE_BYTES) {
      console.warn(`[hal] media too large to inline (${buf.length}B): ${path}`);
      return null;
    }
    const mime = MIME_BY_EXT[extname(path).toLowerCase()] || "application/octet-stream";
    return `data:${mime};base64,${buf.toString("base64")}`;
  } catch (err: any) {
    console.warn(`[hal] could not inline media ${path}: ${err?.message ?? err}`);
    return null;
  }
}

export async function inlineMediaList(urls: string[]): Promise<string[]> {
  const out: string[] = [];
  for (const u of urls) {
    const r = await inlineLocalMedia(u);
    if (r) out.push(r);
  }
  return out;
}
