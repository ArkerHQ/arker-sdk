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
  await mkdir(cacheDirectory, { recursive: true, mode: 0o700 });
  const cached = join(cacheDirectory, artifact.sha256);
  let content: Uint8Array;
  try { content = await readBoundedCache(cached); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    const response = await fetch(artifact.url, { signal: AbortSignal.timeout(60_000), redirect: "error" });
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
    await vm.run(`rm -f -- ${destination}`).catch(() => undefined);
    throw error;
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
