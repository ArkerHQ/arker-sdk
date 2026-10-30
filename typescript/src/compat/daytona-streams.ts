import { randomUUID } from "node:crypto";
import { withRequestTransport } from "../internal/request-context.js";
import { mkdtemp, open, rm, type FileHandle } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { DaytonaConnectionTimeoutError, DaytonaError, type DownloadStreamOptions, type UploadSource, type UploadStreamOptions } from "@daytona/sdk";
import { RequestIdleTimeoutError, type RequestTransportOptions, type VM } from "../index.js";
import { shellQuote } from "./common.js";

type Context = { vm: VM; run(command: string, transport?: RequestTransportOptions): Promise<string>; command(operation: string, path: string): string };

function transport(options: { timeout?: number; signal?: AbortSignal }): RequestTransportOptions {
  const seconds = options.timeout ?? 1800;
  if (!Number.isFinite(seconds) || seconds < 0) throw new RangeError("timeout must be finite and nonnegative");
  return { signal: options.signal, idleTimeoutMs: seconds * 1000 };
}

function transferError(error: unknown): unknown {
  return error instanceof RequestIdleTimeoutError ? new DaytonaConnectionTimeoutError(error.message) : error;
}

function cancelled(signal: AbortSignal | undefined, operation: string, path: string): void {
  if (signal?.aborted) throw new DaytonaError(`${operation} cancelled: ${path}`);
}

type Cleanup = { path: string; close(): unknown | Promise<unknown> };

export async function cleanupTransfer(primary: unknown[], resources: Cleanup[]): Promise<void> {
  const errors = [...primary];
  for (const resource of resources) {
    try { await resource.close(); }
    catch (error) { if (!errors.includes(error)) errors.push(error); }
  }
  if (errors.length === primary.length) {
    if (primary.length) throw primary[0];
    return;
  }
  throw Object.assign(new AggregateError(errors, "Daytona transfer cleanup failed", { cause: primary[0] }), {
    resources: resources.map(resource => resource.path),
  });
}

async function staging(context: Context, options: RequestTransportOptions) {
  const directory = `/tmp/.arker-daytona-fs-${randomUUID()}`;
  try { await context.run(`mkdir -m 700 -- ${shellQuote(directory)}`, options); }
  catch (failure) {
    const error = transferError(failure);
    // A lost response does not prove ownership; report the path without removing it.
    if (error instanceof Error) { Object.assign(error, { stagingDirectory: directory }); throw error; }
    throw Object.assign(new Error(`Staging creation outcome uncertain: ${directory}`, { cause: error }), { stagingDirectory: directory });
  }
  let cleanup: Promise<unknown> | undefined;
  return { path: `${directory}/data`, close: () => cleanup ??= withRequestTransport(undefined, () => context.run(`rm -rf -- ${shellQuote(directory)}`)) };
}

export async function downloadStream(context: Context, path: string, options: DownloadStreamOptions): Promise<Readable> {
  const request = transport(options);
  const command = context.command("download", path);
  cancelled(options.signal, "Download", path);
  const stage = await staging(context, request);
  try { await context.run(`umask 077; set -C; ${command} > ${shellQuote(stage.path)}`, request); }
  catch (error) { await cleanupTransfer([transferError(error)], [stage]); throw error; }
  const controller = new AbortController();
  const chunks = context.vm._syncReadChunks(stage.path, controller.signal, request.idleTimeoutMs);
  let received = 0;
  async function* iterate() {
    const primary: unknown[] = [];
    try {
      for await (const chunk of chunks) {
        cancelled(options.signal, "Download", path);
        received += chunk.length;
        options.onProgress?.({ bytesReceived: received });
        yield Buffer.from(chunk);
      }
    } catch (error) { primary.push(transferError(error)); }
    finally { await cleanupTransfer(primary, [stage]); }
  }
  const iterator = iterate();
  let reading = false;
  const stream = new Readable({
    objectMode: false, highWaterMark: 65536,
    read() {
      if (reading) return;
      reading = true;
      iterator.next().then(({ value, done }) => {
        reading = false;
        if (!this.destroyed) this.push(done ? null : value);
      }, error => { reading = false; this.destroy(error); });
    },
    destroy(error, callback) {
      controller.abort();
      options.signal?.removeEventListener("abort", abort);
      cleanupTransfer(error ? [error] : [], [
        { path: stage.path, close: () => iterator.return() },
        { path: stage.path, close: () => chunks.return(undefined) }, stage,
      ]).then(() => callback(null), failure => callback(failure));
    },
  });
  const abort = () => stream.destroy(new DaytonaError(`Download cancelled: ${path}`));
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();
  return stream;
}

export async function uploadStream(context: Context, source: UploadSource, path: string, options: UploadStreamOptions): Promise<void> {
  const request = transport(options);
  const command = context.command("upload", path);
  cancelled(options.signal, "Upload", path);
  let handle: FileHandle | undefined;
  let directory: string | undefined;
  let stage: Awaited<ReturnType<typeof staging>> | undefined;
  const primary: unknown[] = [];
  let size: number;
  let buffer: Uint8Array | undefined;
  try {
    if (source instanceof Uint8Array) { buffer = source; size = buffer.length; }
    else if (typeof source === "string") {
      handle = await open(source, "r");
      size = (await handle.stat()).size;
    } else {
      // Sync requires a total size; unknown-length sources spool to disk.
      directory = await mkdtemp(join(tmpdir(), "daytona-upload-"));
      handle = await open(join(directory, "data"), "wx+", 0o600);
      const input = source instanceof Readable ? source : Readable.fromWeb(source as never);
      let idle: ReturnType<typeof setTimeout> | undefined;
      const reset = () => {
        clearTimeout(idle);
        if (request.idleTimeoutMs) {
          idle = setTimeout(() => input.destroy(new RequestIdleTimeoutError(request.idleTimeoutMs!)), request.idleTimeoutMs);
          idle.unref?.();
        }
      };
      const abort = () => input.destroy(new DaytonaError(`Upload cancelled: ${path}`));
      options.signal?.addEventListener("abort", abort, { once: true });
      size = 0;
      if (options.signal?.aborted) abort();
      reset();
      try {
        for await (const chunk of input) {
          reset();
          cancelled(options.signal, "Upload", path);
          const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
          await handle.writeFile(data);
          size += data.length;
        }
      } finally { clearTimeout(idle); options.signal?.removeEventListener("abort", abort); }
    }
    stage = await staging(context, request);
    let sent = 0;
    await context.run(`umask 077; set -C; : > ${shellQuote(stage.path)}`, request);
    await context.vm._syncWriteChunks(stage.path, size, async (offset, length) => {
      cancelled(options.signal, "Upload", path);
      let data: Uint8Array;
      if (buffer) data = buffer.subarray(offset, offset + length);
      else {
        const bytes = Buffer.alloc(length);
        let filled = 0;
        while (filled < length) {
          cancelled(options.signal, "Upload", path);
          const read = await handle!.read(bytes, filled, length - filled, offset + filled);
          if (!read.bytesRead) throw new DaytonaError("Upload source ended before its declared size");
          filled += read.bytesRead;
        }
        data = bytes;
      }
      sent += data.length;
      if (data.length) options.onProgress?.({ bytesSent: sent });
      return data;
    }, {}, request);
    cancelled(options.signal, "Upload", path);
    await context.run(`${command} < ${shellQuote(stage.path)}`, request);
  } catch (error) { primary.push(transferError(error)); }
  finally {
    const resources: Cleanup[] = [];
    if (stage) resources.push(stage);
    if (handle) resources.push({ path: directory ? join(directory, "data") : String(source), close: () => handle!.close() });
    if (directory) resources.push({ path: directory, close: () => rm(directory!, { recursive: true }) });
    await cleanupTransfer(primary, resources);
  }
}
