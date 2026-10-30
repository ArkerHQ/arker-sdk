import { SandboxInstance as Native } from '@blaxel/core';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { VM } from '../index.js';
import { privateFetch } from './private-fetch.js';

export interface BlaxelDataPlane {
  readonly fs: Native['fs'];
  readonly process: Native['process'];
  close(): void;
}

export function validateBlaxelNative(): Native {
  const entry = createRequire(typeof import.meta.url === 'string' ? import.meta.url : __filename).resolve('@blaxel/core');
  const version = JSON.parse(readFileSync(join(dirname(entry), '../../package.json'), 'utf8')).version;
  if (version !== '0.3.32') throw new Error('Native Blaxel data plane requires @blaxel/core@0.3.32');
  const native = new Native({ metadata: { name: 'private' }, spec: {}, forceUrl: 'http://localhost:49983', headers: {} });
  for (const component of [native.fs, native.process]) {
    if (typeof component.client?.setConfig !== 'function' || typeof Reflect.get(component, 'h2Fetch') !== 'function')
      throw new Error('Native Blaxel transport structure changed');
  }
  return native;
}

export function createBlaxelDataPlane(vm: Pick<VM, 'connectPort'>): BlaxelDataPlane {
  const native = validateBlaxelNative();
  const origin = 'http://localhost:49983';
  const active = new Set<AbortController>();
  let closed = false;
  const fetch: typeof globalThis.fetch = async (input, init) => {
    if (closed) throw new Error('Native Blaxel data plane is closed');
    const request = new Request(input, init), url = new URL(request.url);
    if (url.origin !== origin) throw new Error('Native daemon request changed origin');
    for (const header of ['authorization', 'x-blaxel-authorization', 'x-blaxel-workspace', 'x-api-key']) request.headers.delete(header);
    const controller = new AbortController();
    const abort = () => controller.abort(request.signal.reason);
    const finish = () => { active.delete(controller); request.signal.removeEventListener('abort', abort); };
    controller.signal.addEventListener('abort', finish, { once: true });
    request.signal.addEventListener('abort', abort, { once: true });
    active.add(controller);
    if (request.signal.aborted) abort();
    try {
      const response = await privateFetch(vm, 49983, url.pathname + url.search, {
        method: request.method, headers: request.headers, body: request.body,
        signal: controller.signal, redirect: 'error', duplex: 'half',
      } as RequestInit);
      if (!response.body) { finish(); return response; }
      const reader = response.body.getReader();
      const wrapped = new Response(new ReadableStream({
        async pull(output) {
          try {
            const next = await reader.read();
            if (next.done) { finish(); reader.releaseLock(); output.close(); }
            else output.enqueue(next.value);
          } catch (error) { finish(); reader.releaseLock(); output.error(error); }
        },
        async cancel(reason) { controller.abort(reason); try { await reader.cancel(reason); } finally { finish(); reader.releaseLock(); } },
      }), { status: response.status, statusText: response.statusText, headers: response.headers });
      Object.defineProperty(wrapped, 'url', { value: response.url });
      return wrapped;
    } catch (error) { finish(); throw error; }
  };
  // Both generated requests and native binary/streaming paths need the same factory.
  for (const component of [native.fs, native.process]) {
    const client = component.client;
    client.setConfig({ fetch });
    Object.defineProperty(component, 'client', { get: () => client });
    Reflect.set(component, 'h2Fetch', fetch);
  }
  return { fs: native.fs, process: native.process,
    close() { closed = true; for (const controller of active) controller.abort(); active.clear(); } };
}
