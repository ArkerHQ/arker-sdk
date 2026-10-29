/** Native envd components; never a vendor control-plane Sandbox handle. */
import { createRequire } from "node:module";
import { Agent as HttpAgent } from "node:http";
import { Agent as HttpsAgent } from "node:https";
import { pathToFileURL } from "node:url";
import type { Commands, Filesystem, Pty, Git } from "e2b";

export interface EnvdDescriptor {
  readonly endpoint: string;
  readonly envd_version: string;
  readonly envd_access_token: string;
}
export interface E2BDataPlane { commands: Commands; files: Filesystem; pty: Pty; git: Git; close(): void }

function assertEndpoint(endpoint: string, input: string): void {
  const base = new URL(endpoint), url = new URL(input);
  if (!["https:", "http:"].includes(base.protocol) || base.username || base.password || base.search || base.hash ||
      url.origin !== base.origin || !url.pathname.startsWith(base.pathname.replace(/\/$/, "") + "/")) {
    throw new Error("Envd transport request is outside its configured endpoint");
  }
}
function paused(status: number, body: Uint8Array): boolean {
  if (status !== 409) return false;
  try {
    const value = JSON.parse(new TextDecoder().decode(body));
    const error = value?.error ?? value;
    return error?.code === "invalid_state" && error?.details?.requirement === "explicit_resume" &&
      error?.details?.resource?.kind === "vm";
  } catch { return false; }
}
const stopped = "Sandbox was not found: explicitly paused; call connect to resume";

const errorLimit = 64 * 1024;

class Requests {
  private closed = false;
  private active = new Map<AbortController, () => void>();
  begin(signal?: AbortSignal) {
    if (this.closed) throw new Error("Envd data plane is closed");
    const controller = new AbortController();
    const finish = () => { this.active.delete(controller); signal?.removeEventListener("abort", abort); };
    const abort = () => { controller.abort(signal?.reason); finish(); };
    this.active.set(controller, finish);
    if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
    return { controller, finish };
  }
  close() {
    this.closed = true;
    for (const [controller, finish] of this.active) { controller.abort(); finish(); }
    this.active.clear();
  }
}

async function readError(chunks: AsyncIterable<Uint8Array>): Promise<{ body: Uint8Array; complete: boolean }> {
  const collected: Uint8Array[] = []; let size = 0;
  for await (const chunk of chunks) {
    const remaining = errorLimit - size;
    collected.push(chunk.subarray(0, remaining));
    size += Math.min(chunk.length, remaining);
    if (chunk.length > remaining) return { body: Buffer.concat(collected, size), complete: false };
  }
  return { body: Buffer.concat(collected, size), complete: true };
}
async function* readableChunks(stream: ReadableStream<Uint8Array> | null) {
  if (!stream) return;
  const reader = stream.getReader();
  try { for (;;) { const next = await reader.read(); if (next.done) return; yield next.value; } }
  finally { try { await reader.cancel(); } finally { reader.releaseLock(); } }
}

/** Credentials and request ownership are scoped to one endpoint, never global fetch. */
export function scopedEnvdFetch(descriptor: EnvdDescriptor, platformKey: string, fetcher: typeof fetch = fetch,
  requests = new Requests()): typeof fetch {
  descriptor = Object.freeze({ ...descriptor });
  return async (input, init) => {
    const request = new Request(input, { ...init, duplex: "half" } as RequestInit);
    assertEndpoint(descriptor.endpoint, request.url);
    const lifetime = requests.begin(request.signal);
    try {
      const headers = new Headers(request.headers);
      headers.set("X-API-Key", platformKey); headers.set("X-Access-Token", descriptor.envd_access_token);
      const response = await fetcher(new Request(request, {
        headers, redirect: "error", signal: lifetime.controller.signal, duplex: "half",
      } as RequestInit));
      if (response.status === 409) {
        try {
          const { body, complete } = await readError(readableChunks(response.body));
          const headers = new Headers(response.headers); headers.delete("content-length");
          return complete && paused(409, body)
            ? new Response(JSON.stringify({ message: stopped }), { status: 502, headers: { "content-type": "application/json" } })
            : new Response(new Uint8Array(body), { status: 409, statusText: response.statusText, headers });
        } finally { lifetime.controller.abort(); lifetime.finish(); }
      }
      if (!response.body) { lifetime.finish(); return response; }
      const reader = response.body.getReader();
      const body = new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            const next = await reader.read();
            if (next.done) { controller.close(); reader.releaseLock(); lifetime.finish(); }
            else controller.enqueue(next.value);
          } catch (error) { lifetime.finish(); reader.releaseLock(); controller.error(error); }
        },
        async cancel(reason) {
          try { await reader.cancel(reason); } finally { lifetime.controller.abort(); lifetime.finish(); reader.releaseLock(); }
        },
      });
      return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
    } catch (error) { lifetime.controller.abort(); lifetime.finish(); throw error; }
  };
}

export async function createE2BDataPlane(descriptor: EnvdDescriptor, platformKey: string, requestTimeoutMs?: number): Promise<E2BDataPlane> {
  descriptor = Object.freeze({ ...descriptor });
  assertEndpoint(descriptor.endpoint, descriptor.endpoint.replace(/\/$/, "") + "/health");
  // These constructors are private native ABI: validate before exposing any component.
  // Optional dependencies load only when this adapter is explicitly requested.
  const isEsm = typeof import.meta.url === "string";
  const require = createRequire(isEsm ? import.meta.url : __filename);
  const vendorEntry = isEsm ? import.meta.resolve("e2b") : require.resolve("e2b");
  const vendorRequire = createRequire(vendorEntry);
  const { Sandbox } = isEsm ? await import(vendorEntry) : require("e2b");
  const { createNodeHttpClient } = require("@connectrpc/connect-node");
  const connectModule = async (name: string) => vendorEntry.endsWith(".mjs")
    ? import(pathToFileURL(vendorRequire.resolve(name).replace("/cjs/", "/esm/")).href)
    : vendorRequire(name);
  const { createTransport } = await connectModule("@connectrpc/connect/protocol-connect");
  const { ConnectError, Code } = await connectModule("@connectrpc/connect");
  const sandbox = new Sandbox({ sandboxId: "arker-data-plane", apiKey: "arker-data-plane-only",
    sandboxUrl: descriptor.endpoint, envdVersion: descriptor.envd_version, requestTimeoutMs });
  if (typeof sandbox.files?.rpc?.stat !== "function" ||
      typeof sandbox.commands?.streamingRpc?.streamInput !== "function" ||
      typeof sandbox.pty?.rpc?.start !== "function" || typeof sandbox.envdApi?.api?.use !== "function") {
    throw new Error("Unsupported native E2B data-plane structure");
  }
  const requests = new Requests();
  const agent = new (new URL(descriptor.endpoint).protocol === "https:" ? HttpsAgent : HttpAgent)({ keepAlive: true });
  const client = createNodeHttpClient({ httpVersion: "1.1", nodeOptions: { agent } });
  const transport = createTransport({
    baseUrl: descriptor.endpoint, useBinaryFormat: false, interceptors: [],
    acceptCompression: [], sendCompression: null, compressMinBytes: 1024,
    readMaxBytes: 0xffffffff, writeMaxBytes: 0xffffffff,
    httpClient: async (request: { url: string; header: Headers; signal?: AbortSignal }) => {
      assertEndpoint(descriptor.endpoint, request.url);
      const lifetime = requests.begin(request.signal);
      request.header.set("X-API-Key", platformKey); request.header.set("X-Access-Token", descriptor.envd_access_token);
      try {
        // Node's HTTP client never follows redirects. Abort explicitly: its body iterator lacks return().
        const response = await client({ ...request, signal: lifetime.controller.signal });
        if (response.status >= 300 && response.status < 400) throw new ConnectError("Envd redirects are refused", Code.PermissionDenied);
        if (response.status === 409) {
          try {
            const { body, complete } = await readError(response.body);
            if (complete && paused(409, body)) throw new ConnectError(stopped, Code.Unavailable);
            const header = new Headers(response.header); header.delete("content-length");
            return { ...response, header, body: (async function* () { yield body; })() };
          } finally { lifetime.controller.abort(); lifetime.finish(); }
        }
        return { ...response, body: (async function* () {
          try { yield* response.body; }
          finally { lifetime.controller.abort(); lifetime.finish(); }
        })() };
      } catch (error) { lifetime.controller.abort(); lifetime.finish(); throw error; }
    },
  });
  const api = new sandbox.envdApi.constructor({ apiUrl: descriptor.endpoint,
    fetch: scopedEnvdFetch(descriptor, platformKey, fetch, requests) }, { version: descriptor.envd_version });
  const files = new sandbox.files.constructor(transport, api, sandbox.connectionConfig);
  const commands = new sandbox.commands.constructor(transport, api, sandbox.connectionConfig, transport);
  const pty = new sandbox.pty.constructor(transport, api, sandbox.connectionConfig, transport);
  const git = new sandbox.git.constructor(commands);
  return { files, commands, pty, git, close() { requests.close(); agent.destroy(); } };
}
