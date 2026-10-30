import { requestTimeout, requestTransport, validateRequestTimeout } from "./e2b-transport.js";
import { InvalidArgumentError, RateLimitError, SandboxError, SandboxNotFoundError, type SandboxForkOpts } from "e2b";
import { ArkerError, type VM } from "../index.js";
import { assertSupportedObjectKeys } from "./common.js";
import { e2bAdmissionError } from "./e2b-errors.js";
import { E2B_LIFETIME_POLICY_ID, e2bTimeoutDuration } from "./e2b-lifecycle.js";
import { trafficPolicies, rememberTrafficToken, managed } from "./e2b-network.js";

function forkError(error: unknown): Error {
  const mapped = e2bAdmissionError(error);
  if (mapped !== error) return mapped as Error;
  if (!(error instanceof ArkerError)) return error instanceof Error ? error : new Error(String(error));
  const Kind = error.status === 400 ? InvalidArgumentError : error.status === 404 ? SandboxNotFoundError
    : error.status === 429 ? RateLimitError : SandboxError;
  const result = new Kind(error.message);
  result.statusCode = error.status;
  return result;
}

/** The checkpoint is immutable at birth and has a server-side deletion deadline.
 * This is not a durable snapshot namespace or a server-side fan-out transaction.
 */
export async function forkArker<T>(vm: VM, prepare: (vm: VM) => Promise<T>, dispose: (value: T) => Promise<unknown>, opts: SandboxForkOpts = {}): Promise<Array<T | Error>> {
  assertSupportedObjectKeys(opts, ["count", "timeoutMs", "requestTimeoutMs", "signal"], "e2b", "Sandbox.fork");
  const count = opts.count ?? 1;
  if (!Number.isInteger(count) || count < 1 || count > 20) throw new InvalidArgumentError("count must be an integer between 1 and 20");
  const seconds = e2bTimeoutDuration(opts.timeoutMs ?? 300_000) / 1000;
  const budget = validateRequestTimeout(opts.requestTimeoutMs ?? requestTimeout(vm));
  if (!Number.isFinite(budget) || budget <= 0) throw new InvalidArgumentError("Fork requires a finite requestTimeoutMs for checkpoint reclamation");
  const deadline = performance.now() + budget;
  const transport = requestTransport(opts, requestTimeout(vm));
  const source = await vm.refresh(transport);
  const metadata = structuredClone(source.metadata ?? null);
  const policies = source.policies ?? undefined;
  const ingress = Object.fromEntries(Object.entries(policies?.rules ?? {}).filter(([, rule]) => rule?.type === "inbound"));
  if ((policies && Object.hasOwn(policies, "policies")) || Object.entries(ingress).some(([id, rule]) => !managed(id, rule))) {
    throw new Error("Fork cannot reconstruct custom inbound policies from redacted responses");
  }
  const publicTraffic = !Object.values(ingress).some(rule => rule?.type === "inbound" && rule.match?.headers?.["e2b-traffic-access-token"]);
  const results: Array<T | Error> = [];
  const pending: Array<VM | Error> = [];
  let checkpoint: VM | undefined;
  try {
    transport.signal?.throwIfAborted();
    checkpoint = await vm.fork({ immutable: true, metadata: { arker_e2b_checkpoint: { version: 1, source_vm_id: vm.id } }, policies: { rules: { ...Object.fromEntries(Object.keys(ingress).map(key => [key, null])), [E2B_LIFETIME_POLICY_ID]: { type: "schedule", after_created: Math.ceil((deadline - performance.now()) / 1000), action: "delete" } } } }, transport);
    for (let index = 0; index < count; index++) {
      transport.signal?.throwIfAborted();
      let child: VM | undefined;
      try {
        const plan = trafficPolicies(policies, { rules: { [E2B_LIFETIME_POLICY_ID]: { type: "schedule", after_created: seconds, action: "delete" } } }, publicTraffic);
        child = await checkpoint.fork({ policies: plan.policies, metadata: structuredClone(metadata) }, transport);
        rememberTrafficToken(child, plan.token);
        transport.signal?.throwIfAborted();
        pending.push(child);
      } catch (error) {
        if (child) {
          try { await child.delete(); }
          catch (cleanup) { error = new AggregateError([error, cleanup], "Fork initialization and child cleanup failed"); }
        }
        if (transport.signal?.aborted) throw error;
        pending.push(forkError(error));
      }
    }
    transport.signal?.throwIfAborted();
    await checkpoint.delete();
    checkpoint = undefined;
    while (pending.length) {
      const child = pending.shift()!;
      if (child instanceof Error) { results.push(child); continue; }
      try {
        transport.signal?.throwIfAborted();
        results.push(await prepare(child));
      } catch (error) {
        try { await child.delete(); }
        catch (cleanup) { error = new AggregateError([error, cleanup], "Fork initialization and child cleanup failed"); }
        if (transport.signal?.aborted) throw error;
        results.push(forkError(error));
      }
    }
    transport.signal?.throwIfAborted();
    return results;
  } catch (error) {
    const failures: unknown[] = [error];
    for (const value of results) {
      if (!(value instanceof Error)) {
        try { await dispose(value); } catch (cleanup) { failures.push(cleanup); }
      }
    }
    for (const child of pending) {
      if (!(child instanceof Error)) {
        try { await child.delete(); } catch (cleanup) { failures.push(cleanup); }
      }
    }
    if (checkpoint) {
      try { await checkpoint.delete(); } catch (cleanup) { failures.push(cleanup); }
    }
    if (failures.length > 1) throw new AggregateError(failures, "Fork failed and cleanup was incomplete");
    throw forkError(error);
  }
}
