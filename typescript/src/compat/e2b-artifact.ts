/** Client-side staging. Pass the returned candidate as artifact_path to guest ensure. */
import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, rename, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { VM } from "../index.js";

export interface EnvdArtifact { readonly url: string; readonly sha256: string }
const maxBytes = 128 * 1024 * 1024;
export async function stageEnvdArtifact(vm: Pick<VM, "sync" | "run">, rootPath: string,
  artifact: EnvdArtifact, cacheDirectory: string): Promise<string> {
  if (rootPath !== "/root/.arker/e2b-envd" || !/^[a-f0-9]{64}$/.test(artifact.sha256) ||
      !/^https:\/\/storage\.googleapis\.com\/e2b-artifact-binaries\/envd\/v0\.10\.2\/(arm64\/)?envd$/.test(artifact.url)) {
    throw new Error("Invalid pinned envd artifact");
  }
  return stagePinnedArtifact(vm, rootPath, artifact, cacheDirectory);
}

export async function stagePinnedArtifact(vm: Pick<VM, "sync" | "run">, rootPath: string,
  artifact: EnvdArtifact, cacheDirectory: string): Promise<string> {
  await mkdir(cacheDirectory, { recursive: true, mode: 0o700 });
  const cached = join(cacheDirectory, artifact.sha256);
  let content: Uint8Array;
  try { content = await readBoundedCache(cached); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    const response = await downloadArtifact(artifact.url);
    if (!response.ok || !response.body) throw new Error("Envd artifact download failed");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = []; let size = 0;
    try {
      for (;;) {
        const next = await reader.read(); if (next.done) break;
        size += next.value.length;
        if (size > maxBytes) throw new Error("Envd artifact download limit exceeded");
        chunks.push(next.value);
      }
    } finally { await reader.cancel(); reader.releaseLock(); }
    content = Buffer.concat(chunks);
    verify(content, artifact.sha256);
    const temporary = `${cached}.${randomUUID()}`;
    try { await writeFile(temporary, content, { flag: "wx", mode: 0o600 }); await rename(temporary, cached); }
    finally { await unlink(temporary).catch(() => undefined); }
  }
  verify(content, artifact.sha256);
  // Only the root-locked guest helper may install this candidate at the final hash path.
  const destination = `${rootPath}/bin/.candidate-${randomUUID()}`;
  try {
    await vm.sync(destination, content);
    const result = await vm.run(`chmod 0700 -- ${destination}`);
    if (result.exitCode !== 0) throw new Error("Cannot set envd artifact permissions");
    return destination;
  } catch (error) {
    // Until the candidate is returned, the caller cannot hand cleanup to ensure.
    try {
      const cleanup = await vm.run(`rm -f -- ${destination}`);
      if (cleanup.exitCode !== 0) throw new Error("Artifact staging cleanup failed");
    } catch (cleanupError) {
      throw new AggregateError([error, cleanupError], "Artifact staging and cleanup failed");
    }
    throw error;
  }
}
async function downloadArtifact(source: string): Promise<Response> {
  const signal = AbortSignal.timeout(60_000);
  const release = /^https:\/\/github\.com\/blaxel-ai\/sandbox\/releases\/download\/v0\.2\.74\/sandbox-api-linux-(amd64|arm64)$/.test(source);
  let url = source;
  for (let redirects = 0; ; redirects++) {
    // Artifact downloads carry no platform or vendor credentials.
    const response = await fetch(url, { signal, redirect: "manual", credentials: "omit" });
    if (![301, 302, 303, 307, 308].includes(response.status)) return response;
    await response.body?.cancel();
    const location = response.headers.get("location");
    if (!release || redirects >= 3 || !location) throw new Error("Pinned artifact redirect rejected");
    const next = new URL(location, url);
    if (next.protocol !== "https:" || next.hostname !== "release-assets.githubusercontent.com" ||
        next.port || next.username || next.password) throw new Error("Pinned artifact redirect rejected");
    url = next.href;
  }
}

function verify(content: Uint8Array, digest: string): void {
  if (content.length > maxBytes || createHash("sha256").update(content).digest("hex") !== digest) {
    throw new Error("Envd artifact checksum mismatch");
  }
}

async function readBoundedCache(path: string): Promise<Uint8Array> {
  const file = await open(path, "r");
  const chunks: Uint8Array[] = []; let size = 0;
  try {
    for (;;) {
      const buffer = Buffer.alloc(Math.min(512 * 1024, maxBytes + 1 - size));
      const { bytesRead } = await file.read(buffer);
      if (!bytesRead) return Buffer.concat(chunks);
      size += bytesRead;
      if (size > maxBytes) throw new Error("Envd artifact cache size limit exceeded");
      chunks.push(buffer.subarray(0, bytesRead));
    }
  } finally { await file.close(); }
}
