import { readFile } from "node:fs/promises";
import { ConnectionConfig, Sandbox as NativeSandbox, type SandboxInfo, type SandboxListOpts, type SandboxApiOpts } from "e2b";
import { Arker, ArkerError } from "../index.js";
import { assertSupportedObjectKeys, type UniversalSandbox } from "./common.js";
import { parseRouterConfig } from "./router.js";
import { E2BLifecycle, lifecycleTransport } from "./e2b-lifecycle.js";

export type E2BListOptions = SandboxListOpts & { routerConfigPath?: string };
export type E2BPaginator = Pick<ReturnType<typeof NativeSandbox.list>, "hasNext" | "nextToken" | "nextItems">;
function unsupported(message: string): never { throw new ArkerError("unsupported_operation", message, 422); }

/** Server cursors and metadata filtering; never bootstrap or resume listed sandboxes. */
class ArkerPaginator implements E2BPaginator {
  private more = true;
  private cursor?: string;
  private readonly seen = new Set<string>();
  private busy = false;
  private readonly client = new Arker();
  constructor(private readonly options: SandboxListOpts) {
    assertSupportedObjectKeys(options, ["query", "limit", "nextToken", "requestTimeoutMs", "order"], "e2b", "Sandbox.list");
    assertSupportedObjectKeys(options.query, ["metadata", "state", "template", "startedAfter"], "e2b", "Sandbox.list.query");
    if (options.limit !== undefined && (!Number.isSafeInteger(options.limit) || options.limit <= 0)) throw new TypeError("limit must be a positive integer");
    if (options.nextToken !== undefined && (typeof options.nextToken !== "string" || !options.nextToken)) throw new TypeError("nextToken must be a nonempty string");
    const metadata = options.query?.metadata;
    if (metadata !== undefined && (!metadata || typeof metadata !== "object" || Array.isArray(metadata) || Object.values(metadata).some(value => typeof value !== "string"))) throw new TypeError("metadata must be a string-to-string object");
    const state = options.query?.state;
    if (state !== undefined && (!Array.isArray(state) || state.some(value => value !== "running" && value !== "paused"))) throw new TypeError("state must contain running or paused");
    if (options.query?.template !== undefined && typeof options.query.template !== "string") throw new TypeError("template must be a string");
    if (options.order !== undefined && options.order !== "asc" && options.order !== "desc") throw new TypeError("order must be asc or desc");
    this.cursor = options.nextToken;
  }
  get hasNext() { return this.more; }
  get nextToken() { return this.cursor; }
  async nextItems(options?: SandboxApiOpts): Promise<SandboxInfo[]> {
    assertSupportedObjectKeys(options, ["requestTimeoutMs", "signal"], "e2b", "Sandbox.list.nextItems");
    if (!this.more) throw new Error("No more sandbox pages");
    if (this.busy) throw new Error("A sandbox page request is already in progress");
    this.busy = true;
    try {
      const query = this.options.query;
      const transport = lifecycleTransport(ConnectionConfig.mergeOpts(this.options, options));
      const requestOptions = { requestTimeoutMs: 0, signal: transport.signal };
      const parameters = { cursor: this.cursor, limit: this.options.limit ?? 100,
        started_at_order: this.options.order ?? ("desc" as const),
        ...(query?.startedAfter != null ? { started_after: query.startedAfter.toISOString() } : {}),
        metadata_filter: JSON.stringify({ arker_e2b: { version: 1,
          ...(query?.metadata !== undefined ? { metadata: query.metadata } : {}),
          ...(query?.template !== undefined ? { source: { value: query.template } } : {}),
        } }),
      };
      const page = await this.client.listVms(parameters, transport);
      if (page.nextCursor && (page.nextCursor === this.cursor || this.seen.has(page.nextCursor))) throw new Error("Repeated sandbox pagination cursor");
      const results: SandboxInfo[] = [];
      for (const vm of page.vms) {
        const box = { provider: "arker", sandboxId: vm.id, getInstance: () => vm } as unknown as UniversalSandbox;
        const lifecycle = new E2BLifecycle(box);
        if (query?.state) {
          const state = await lifecycle.isRunning(requestOptions) ? "running" : "paused";
          if (!query.state.includes(state)) continue;
        }
        results.push(await lifecycle.getInfo(requestOptions));
      }
      if (page.nextCursor) this.seen.add(page.nextCursor);
      this.cursor = page.nextCursor ?? undefined;
      this.more = page.nextCursor !== null;
      return results;
    } finally { this.busy = false; }
  }
}

export function listSandboxes(options: E2BListOptions = {}): E2BPaginator {
  const { routerConfigPath, ...opts } = options;
  const path = routerConfigPath ?? process.env.COMPUTESDK_ROUTER_CONFIG;
  const arker = !!process.env.ARKER_API_KEY?.trim();
  if (!path) {
    if (!arker) return NativeSandbox.list(opts);
    if (process.env.E2B_API_KEY?.trim()) unsupported("Mixed-provider sandbox listing requires a router with one inventory provider");
    return new ArkerPaginator(opts);
  }
  let delegate: E2BPaginator | undefined;
  let initializing: Promise<E2BPaginator> | undefined;
  const initialize = () => initializing ??= readFile(path, "utf8").then(contents => {
    const router = parseRouterConfig(JSON.parse(contents), arker ? ["arker", "e2b"] : ["e2b"]);
    if (router.existing.order.length !== 1) unsupported("Mixed-provider sandbox listing is not supported");
    return delegate = router.existing.order[0] === "arker" ? new ArkerPaginator(opts) : NativeSandbox.list(opts);
  });
  return {
    get hasNext() { return delegate?.hasNext ?? true; },
    get nextToken() { return delegate?.nextToken ?? opts.nextToken; },
    async nextItems(callOptions) { return (await initialize()).nextItems(callOptions); },
  };
}
