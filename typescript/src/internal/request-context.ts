import { AsyncLocalStorage } from "node:async_hooks";
import type { RequestTransportOptions } from "../index.js";

type Transport = RequestTransportOptions;
const requests = new AsyncLocalStorage<Transport | undefined>();

export function currentRequestTransport(): Transport | undefined { return requests.getStore(); }

export function withRequestTransport<T>(transport: Transport | undefined, operation: () => Promise<T>): Promise<T> {
  return requests.run(transport, operation);
}
