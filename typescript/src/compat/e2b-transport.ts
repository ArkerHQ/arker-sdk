import { ConnectionConfig } from "e2b";
import type { Arker, RequestTransportOptions, VM } from "../index.js";
interface Defaults { requestTimeoutMs: number; signal?: AbortSignal }
const defaults = new WeakMap<Arker, Defaults>();
export function requestTimeout(vm: Pick<VM, "_client">): number {
  return defaults.get(vm._client)?.requestTimeoutMs ?? 60_000;
}
export function validateRequestTimeout(value: number | undefined): number {
  const duration = value ?? 60_000;
  if (typeof duration !== "number") throw new TypeError("requestTimeoutMs must be a number");
  if (!Number.isFinite(duration) || duration < 0 || !Number.isInteger(duration)) {
    throw new RangeError("requestTimeoutMs must be a finite non-negative integer");
  }
  return duration;
}
export function requestTransport(opts?: { requestTimeoutMs?: number; signal?: AbortSignal }, fallback = 60_000): RequestTransportOptions {
  const duration = validateRequestTimeout(opts?.requestTimeoutMs ?? fallback);
  return { signal: new ConnectionConfig({ requestTimeoutMs: duration }).getSignal(undefined, opts?.signal) };
}
/** Each private client request gets a fresh native timeout; caller cancellation is scoped to initialization. */
export function configureTransport(client: Arker, requestTimeoutMs?: number, signal?: AbortSignal): void {
  const installed = defaults.has(client);
  defaults.set(client, { requestTimeoutMs: validateRequestTimeout(requestTimeoutMs), signal });
  if (installed) return;
  const original = client._request.bind(client);
  const download = client._download.bind(client);
  client._download = (url, transport) => download(url, transport ?? requestTransport(defaults.get(client)!));
  client._request = function <T>(...args: Parameters<Arker["_request"]>): Promise<T> {
    const config = defaults.get(client)!;
    const transport = { ...(args[7] ?? requestTransport(config)) };
    if (config.signal && transport.signal !== config.signal) {
      transport.signal = transport.signal ? AbortSignal.any([transport.signal, config.signal]) : config.signal;
    }
    args[7] = transport;
    return original<T>(...args);
  };
}
export function finishInitialization(vm: Pick<VM, "_client">): void {
  const config = defaults.get(vm._client);
  if (config) config.signal = undefined;
}
