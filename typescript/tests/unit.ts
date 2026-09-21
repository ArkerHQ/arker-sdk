import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import nodePath from "node:path";
import { createHash } from "node:crypto";

import {
  Arker,
  ArkerError,
  discoverRegions,
  type CompletedRunResult,
  type PtyWebSocketFactory,
} from "../src/index.js";
import { runPollBudgetMs } from "../src/internal/run-poll.js";
import { isExpectedSurfaceStub } from "./helpers/surface-errors.js";

type FetchCall = {
  url: string;
  method: string;
  body?: string;
  /** Raw request body, buffered for the binary upload paths. */
  bodyBytes?: Uint8Array;
  headers: Record<string, string>;
};

type FetchScript = {
  predicate: (method: string, url: string) => boolean;
  respond: (call: FetchCall) => Response;
  /** If true, stays in the script and can match every subsequent call
   * instead of being consumed after one match. */
  sticky?: boolean;
};

class FakeFetch {
  readonly calls: FetchCall[] = [];
  private readonly script: FetchScript[] = [];

  addJson(predicate: FetchScript["predicate"], status: number, body: unknown): void {
    this.script.push({
      predicate,
      respond: () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
    });
  }

  /**
   * Like `addJson`, but computes the response from the ACTUAL request (e.g.
   * echoing back how many chunks a `/sync` write batch carried), and stays
   * available to match every subsequent matching call rather than being
   * consumed after one — for flows whose exact number of requests is a
   * property under test, not something the test should have to predict.
   */
  addDynamicJson(
    predicate: FetchScript["predicate"],
    respond: (call: FetchCall) => { status: number; body: unknown },
  ): void {
    this.script.push({
      predicate,
      sticky: true,
      respond: (call) => {
        const { status, body } = respond(call);
        return new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });
      },
    });
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? init.body : undefined;
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries((init?.headers ?? {}) as Record<string, string>)) {
      headers[key.toLowerCase()] = value;
    }
    // Buffer binary bodies too, so upload tests can inspect what was packed.
    let bodyBytes: Uint8Array | undefined;
    const raw = init?.body as unknown;
    if (raw instanceof Uint8Array) bodyBytes = raw;
    else if (raw && typeof (raw as ReadableStream).getReader === "function") {
      const chunks: Uint8Array[] = [];
      const reader = (raw as ReadableStream<Uint8Array>).getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) chunks.push(value);
      }
      bodyBytes = new Uint8Array(Buffer.concat(chunks.map((c) => Buffer.from(c))));
    }
    const call: FetchCall = { url, method, body, bodyBytes, headers };
    this.calls.push(call);

    const index = this.script.findIndex((entry) => entry.predicate(method, url));
    assert.notEqual(index, -1, `no scripted response for ${method} ${url}`);
    const entry = this.script[index]!;
    if (!entry.sticky) this.script.splice(index, 1);
    return entry.respond(call);
  };
}

class FakeWebSocket {
  binaryType = "";
  readyState = 1;
  readonly sent: Array<string | Uint8Array | ArrayBuffer> = [];
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>();

  send(data: string | Uint8Array | ArrayBuffer): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.emit("close", { code, reason });
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(listener);
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function client(fetch: FakeFetch): Arker {
  return new Arker({
    apiKey: "ark_live_test",
    baseUrl: "https://test.invalid/api/",
    fetch: fetch.fetch,
    retry: false,
  });
}

function regionClient(fetch: FakeFetch): Arker {
  return new Arker({
    apiKey: "ark_live_test",
    provider: "provider-one",
    region: "region-one",
    fetch: fetch.fetch,
    retry: false,
  });
}

// Every other client() in this file hardcodes retry:false, so the retry
// loop itself (index.ts's `for (attempt < this.retry.attempts)`) had zero
// coverage. Near-zero delays keep the retry tests fast.
function clientWithRetry(fetch: FakeFetch, attempts: number): Arker {
  return new Arker({
    apiKey: "ark_live_test",
    baseUrl: "https://test.invalid/api/",
    fetch: fetch.fetch,
    retry: { attempts, baseDelayMs: 1, maxDelayMs: 1, jitterMs: 0 },
  });
}

const RETRYABLE_ERROR_BODY = {
  error: { code: "unavailable", message: "temporarily unavailable", timestamp: "2026-01-01T00:00:00.000Z" },
};

function decode(bytes: Uint8Array): string {
  return new TextDecoder().decode(bytes);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function testForkPostsDirectlyToSourceVm(): Promise<void> {
  const fetch = new FakeFetch();
  // Contract 0.3: forks go to the top-level `/v1/fork` endpoint and
  // pass the source vm id in the body. `Computer.fork()` (the legacy
  // ergonomic) auto-populates `source_vm_id` from the owning Computer.
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    {
      vm_id: "vm_child",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
    },
  );

  const vm = await client(fetch).vm("source-vm-id").fork({
    name: "demo",
    description: "CI runner",
    ssh_public_keys: ["ssh-ed25519 AAAA test@example.com"],
  });

  assert.equal(vm.id, "vm_child");
  assert.deepEqual(
    JSON.parse(fetch.calls[0]!.body!),
    {
      name: "demo",
      description: "CI runner",
      ssh_public_keys: ["ssh-ed25519 AAAA test@example.com"],
      source_vm_id: "source-vm-id",
    },
  );
}

// fork() forwards the caller's fields instead of naming each one. The old
// allowlist accepted any contract field at the type level and dropped the ones
// it didn't enumerate, so asking for a disk-only fork silently produced a full
// warm fork — a wrong result with no error. These pin the passthrough AND the
// exclusions, since forwarding a non-contract key is a 400 from the server.

async function testForkForwardsContractFieldsItDoesNotEnumerate(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    { vm_id: "vm_child", owner_org_id: "o", created_at: "now", public: false, state: "idle", sessions: [] },
  );

  await client(fetch).fork("source-vm", { layers: ["disk"] });

  const body = JSON.parse(fetch.calls[0]!.body!);
  assert.deepEqual(body.layers, ["disk"], "layers must reach the wire, not be dropped");
}

// `image` is a third fork source. The service models the three as a `oneOf`,
// so a body carrying `image` alongside a VM selector fails to decode there —
// and a body that sent a REAL selector next to an image would turn a registry
// pull into a fork of someone's VM.
async function testForkFromImageSendsOnlyTheImage(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    { vm_id: "vm_img", owner_org_id: "o", created_at: "now", public: false, state: "idle", sessions: [] },
  );

  await client(fetch).fork({ image: "ubuntu:24.04", name: "from-image" });

  const body = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(body.image, "ubuntu:24.04", "image must reach the wire");
  assert.equal(body.name, "from-image");
  assert.equal(body.source_vm_id, null, "no VM selector may accompany an image");
  assert.equal(body.source_vm_name, null, "no VM selector may accompany an image");
}

async function testForkRejectsImageAlongsideASourceVm(): Promise<void> {
  const fetch = new FakeFetch();
  for (const src of [
    { image: "ubuntu:24.04", sourceVmName: "base" },
    { image: "ubuntu:24.04", sourceVmId: "vm_abc" },
  ]) {
    let threw = false;
    try {
      await client(fetch).fork(src);
    } catch (err) {
      threw = true;
      assert.equal((err as { code?: string }).code, "bad_request");
    }
    assert.equal(threw, true, `${JSON.stringify(src)} must be refused before any request`);
  }
  assert.equal(fetch.calls.length, 0, "a refused fork must not reach the network");
}

async function testForkRejectsUnsupportedFlatResourceInputs(): Promise<void> {
  const fetch = new FakeFetch();
  await assert.rejects(
    () => client(fetch).fork({
      sourceVmName: "source-vm",
      vcpu_count: 2,
    } as never),
    (error: unknown) =>
      error instanceof ArkerError &&
      error.code === "bad_request" &&
      error.message.includes("use resources"),
  );
  assert.equal(fetch.calls.length, 0);
}

async function testForkOmitsSourceOrgWhenNotExplicit(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    {
      vm_id: "vm_named_source",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
      network: { ssh_public_keys: [] },
      resources: { vcpu: 4, memory_mib: 8192, disk_mib: 10240 },
    },
  );

  const vm = await client(fetch).fork("catalog-template", {
    ssh_public_keys: ["ssh-ed25519 AAAA test@example"],
    policies: { policies: [] },
  });

  assert.equal(vm.id, "vm_named_source");
  const body = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(body.source_vm_name, "catalog-template");
  assert.equal(body.source_org_id, undefined);
  assert.equal(body.disk, undefined);
  assert.equal(body.platforms, undefined);
  assert.deepEqual(body.ssh_public_keys, ["ssh-ed25519 AAAA test@example"]);
  assert.deepEqual(body.policies, { policies: [] });
  assert.equal(body.network, undefined);
  assert.equal(body.egress, undefined);
}

async function testForkOmitsUnconfiguredCapabilities(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) =>
      method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    {
      vm_id: "vm_plain",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
    },
  );

  await client(fetch).fork("catalog-template");

  const body = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(body.policies, undefined);
  assert.equal(body.ssh_public_keys, undefined);
}

async function testRemovedNetworkInputsFailBeforeRequests(): Promise<void> {
  const fetch = new FakeFetch();
  const isBadRequest = (error: unknown) =>
    error instanceof ArkerError &&
    error.code === "bad_request" &&
    error.status === 400 &&
    error.message.includes("use policies");

  await assert.rejects(
    () =>
      client(fetch).fork(
        "source-vm",
        { network: { reachable: false } } as never,
      ),
    isBadRequest,
  );
  await assert.rejects(
    () => client(fetch).vm("vm_1").fork({ egress: "blocked" } as never),
    isBadRequest,
  );
  await assert.rejects(
    () =>
      client(fetch).vm("vm_1").run(
        "curl https://example.com",
        { network: { inbound: null } } as never,
      ),
    isBadRequest,
  );
  assert.equal(fetch.calls.length, 0);
}

async function testUpdateSendsTopLevelSshPublicKeys(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "PATCH" && url.endsWith("/v1/vms/vm_1"),
    200,
    {
      vm_id: "vm_1",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
      resources: {},
      network: {},
    },
  );

  await client(fetch).vm("vm_1").update({ ssh_public_keys: [] });

  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { ssh_public_keys: [] });
}

async function testNestedErrorWithoutOkStillParses(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "DELETE" && url === "https://test.invalid/api/v1/vms/missing",
    503,
    {
      error: {
        code: "unavailable",
        message: "try later",
        timestamp: "2026-07-21T00:00:00Z",
      },
    },
  );

  await assert.rejects(
    client(fetch).vm("missing").delete(),
    (error) => error instanceof ArkerError && error.code === "unavailable" && error.status === 503,
  );
}

async function testCompletedRunDecodesOutput(): Promise<void> {
  const fetch = new FakeFetch();
  // Contract 0.3: per-VM runs go to `/runs` (plural). The legacy
  // `/run` (singular) endpoint is still wired on the backend as an
  // alias, but the SDK targets the new path.
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    {
      stdout: "hello\n",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: 0,
      memory_requested_mib: 1024,
      memory_achieved_mib: 1536,
      memory_partial: true,
    },
  );

  const result = await client(fetch).vm("vm_1").run("printf hello");

  assert.equal(result.type, "completed");
  const completed = result as CompletedRunResult;
  assert.equal(completed.exitCode, 0);
  assert.equal(completed.memoryRequestedMib, 1024);
  assert.equal(completed.memoryAchievedMib, 1536);
  assert.equal(completed.memoryPartial, true);
  assert.equal(completed.stdout, "hello\n");
  assert.deepEqual(completed.stdoutBytes, new TextEncoder().encode("hello\n"));
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { command: "printf hello" });
}

async function testSyncRunPollsBackgroundedRunToCompletion(): Promise<void> {
  // A synchronous run() that outlives the server sync window gets a background
  // ack; run() must poll getRun() under the hood and resolve to the terminal
  // run — the caller never sees the intermediate background shape.
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    { run_id: "run_bg", state: "running" },
  );
  // First poll: still running. Second poll: terminal.
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/runs/run_bg",
    200,
    { run_id: "run_bg", state: "running", started_at: "now", exit_code: null, stdout: "", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8" },
  );
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/runs/run_bg",
    200,
    { run_id: "run_bg", state: "completed", started_at: "now", exit_code: 0, stdout: "done\n", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8" },
  );

  const result = await client(fetch).vm("vm_1").run("sleep 999");

  assert.equal(result.type, "completed");
  const completed = result as CompletedRunResult;
  assert.equal(completed.runId, "run_bg");
  assert.equal(completed.state, "completed");
  assert.equal(completed.exitCode, 0);
  assert.equal(completed.stdout, "done\n");
  assert.deepEqual(completed.stdoutBytes, new TextEncoder().encode("done\n"));
  // POST + 2 polls.
  assert.equal(fetch.calls.length, 3);
  assert.equal(fetch.calls[0]!.method, "POST");
  assert.equal(fetch.calls[1]!.method, "GET");
  assert.equal(fetch.calls[2]!.method, "GET");
}

async function testRunPollBudgetIsUnboundedWithoutACallerTimeout(): Promise<void> {
  // The poll budget exists to outlive the server-side kill and report its
  // outcome. There is no server-side kill without a caller `timeout` (absent
  // and `0` are both unbounded), so there is nothing to outlive and the poll
  // must not invent a deadline — abandoning a run that is still going is worse
  // than waiting.
  assert.equal(runPollBudgetMs(undefined), null);
  assert.equal(runPollBudgetMs(null), null);
  assert.equal(runPollBudgetMs(0), null);
  // A caller-set bound still gets the kill bound plus the 30s margin.
  assert.equal(runPollBudgetMs(5), 5_000 + 30_000);
  assert.equal(runPollBudgetMs(3_600), 3_600_000 + 30_000);
  // Negative is nonsense the API would reject; treat it as unbounded rather
  // than as an instantly-expired deadline.
  assert.equal(runPollBudgetMs(-1), null);
}

async function testExplicitZeroReturnsAckWithoutPolling(): Promise<void> {
  // time_to_background:0 is a pure pass-through — run() returns the running ack
  // immediately and never polls getRun().
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    { run_id: "run_bg", state: "running" },
  );

  const result = await client(fetch).vm("vm_1").run("sleep 999", { time_to_background: 0 });

  assert.equal(result.type, "background");
  assert.equal((result as { runId: string }).runId, "run_bg");
  // Only the POST — no polling.
  assert.equal(fetch.calls.length, 1);
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { command: "sleep 999", time_to_background: 0 });
}

async function testConfiguredPlacementRoutesNamedSourcesToMainEndpoint(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://provider-one-region-one.arker.ai/api/v1/fork",
    200,
    {
      vm_id: "vmh-child",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
    },
  );

  const arker = regionClient(fetch);
  const vm = await arker.vm("source-vm-id").fork();

  assert.equal(arker.baseUrl, "https://provider-one-region-one.arker.ai/api");
  assert.equal(vm.baseUrl, "https://provider-one-region-one.arker.ai/api");
}

function testArbitraryProviderBuildsEndpoint(): void {
  const arker = new Arker({
    apiKey: "ark_live_test",
    provider: "future-cloud",
    region: "moon-1",
    retry: false,
  });

  assert.equal(arker.provider, "future-cloud");
  assert.equal(arker.region, "moon-1");
  assert.equal(arker.baseUrl, "https://future-cloud-moon-1.arker.ai/api");
}

function testPlacementRequiresSeparateProviderAndRegion(): void {
  assert.throws(
    () => new Arker({ apiKey: "ark_live_test", region: "region-one" }),
    /provider and region are required together/i,
  );
  assert.throws(
    () => new Arker({ apiKey: "ark_live_test", provider: "provider-one" }),
    /provider and region are required together/i,
  );
}

function testInvalidProviderSyntaxFailsClosed(): void {
  assert.throws(
    () => new Arker({ apiKey: "ark_live_test", provider: "bad.example/path", region: "region-one" }),
    /provider/i,
  );
}

async function testListRegionsUsesPublicControlPlaneCatalog(): Promise<void> {
  const fetch = new FakeFetch();
  const placement = {
    provider: "provider-two",
    region: "region-two",
  };
  fetch.addJson(
    (method, url) =>
      method === "GET" && url === "https://control.invalid/api/v1/regions",
    200,
    { regions: [placement] },
  );
  const arker = new Arker({
    apiKey: "ark_live_test",
    provider: "aws",
    region: "us-west-2",
    controlBaseUrl: "https://control.invalid/api",
    fetch: fetch.fetch,
    retry: false,
  });

  assert.deepEqual(await arker.listRegions(), { regions: [placement] });
}

async function testDiscoverRegionsRequiresNoConfiguredClient(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) =>
      method === "GET" && url === "https://control.invalid/api/v1/regions",
    200,
    { regions: [] },
  );

  assert.deepEqual(
    await discoverRegions({
      controlBaseUrl: "https://control.invalid/api",
      fetch: fetch.fetch,
      retry: false,
    }),
    { regions: [] },
  );
  assert.equal(fetch.calls[0]?.headers.authorization, undefined);
}

async function testListedVmUsesItsPlacementEndpoint(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://arker.ai/api/v1/vms",
    200,
    {
      vms: [{
        vm_id: "vm_placed",
        owner_org_id: "org_1",
        created_at: "now",
        region: "region-two",
        provider: "provider-two",
      }],
    },
  );
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://provider-two-region-two.arker.ai/api/v1/vms/vm_placed/runs",
    200,
    {
      run_id: "run_placed",
      state: "completed",
      stdout: "ok\n",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: 0,
    },
  );

  const arker = new Arker({
    apiKey: "ark_live_test",
    baseUrl: "https://fallback.invalid/api",
    fetch: fetch.fetch,
    retry: false,
  });
  const { vms } = await arker.listVms();
  assert.equal(vms[0]?.baseUrl, "https://provider-two-region-two.arker.ai/api");
  await vms[0]!.run("printf ok");
}

function testExplicitVmHandleUsesPlacementEndpoint(): void {
  const arker = new Arker({
    apiKey: "ark_live_test",
    baseUrl: "https://fallback.invalid/api",
    retry: false,
  });
  const vm = arker.vm("vm_placed", { provider: "provider-two", region: "region-two" });
  assert.equal(vm.baseUrl, "https://provider-two-region-two.arker.ai/api");
}

async function testListRunsUsesControlPlaneAndFilters(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://control.invalid/api/v1/runs?since=10&until=20&vm=vm_1&vms=vm_2%2Cvm_3&region=us-west-2&provider=aws&search=pytest&limit=25&offset=5&lite=true&runtime=vm&endpoint=run&actions=run%2Cfork&status=success%2Cinternal&status_min=200&status_max=599&sort=when&dir=asc",
    200,
    {
      since: 10,
      until: 20,
      limit: 25,
      offset: 5,
      lite: true,
      rows: [{
        t_ms: 10,
        request_id: "req_1",
        run_id: "run_1",
        vm_id: "vm_1",
        session_id: "session_1",
        region: "us-west-2",
        status: 200,
        total_ms: 12.5,
        queue_ms: 1.5,
        executor_duration_ms: 10,
        executor_kind: "vm",
        executor_cpu_ms: 8,
        executor_mem_mb: 64,
        vm_vcpus: 2,
        vm_memory_mib: 4096,
        path: "/v1/vms/vm_1/runs",
        method: "POST",
        command: "pytest",
        source_vm_id: "",
        exit_code: 0,
        endpoint: "run",
        api_key_prefix: "ark_live",
        body_bytes_in: 10,
        body_bytes_out: 20,
        body_in: "",
        body_out: "",
      }],
    },
  );

  const arker = new Arker({
    apiKey: "ark_live_test",
    baseUrl: "https://test.invalid/api/",
    controlBaseUrl: "https://control.invalid/api/",
    fetch: fetch.fetch,
    retry: false,
  });
  const result = await arker.listRuns({
    since: 10,
    until: 20,
    vm: "vm_1",
    vmIds: ["vm_2", "vm_3"],
    region: "us-west-2",
    provider: "aws",
    search: "pytest",
    limit: 25,
    offset: 5,
    lite: true,
    runtime: "vm",
    endpoint: "run",
    actions: ["run", "fork"],
    status: ["success", "internal"],
    statusMin: 200,
    statusMax: 599,
    sort: "when",
    dir: "asc",
  });

  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0]!.region, "us-west-2");
  assert.equal(result.rows[0]!.vm_vcpus, 2);
  assert.equal(result.lite, true);
  assert.equal(fetch.calls[0]!.method, "GET");
  assert.equal(fetch.calls[0]!.body, undefined);
}

async function testListVmsPreservesForkLimitFields(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://arker.ai/api/v1/vms?region=us-west-2&provider=aws&org_id=ArkerHQ&public=true&state=idle",
    200,
    {
      vms: [{
        vm_id: "vm_1",
        owner_org_id: "ArkerHQ",
        created_at: "now",
        public: true,
        state: "idle",
        sessions: [],
        network: { ssh_public_keys: [] },
        max_vcpus: 8,
        max_memory_mib: 32768,
        min_memory_mib: 512,
      }],
    },
  );

  const result = await client(fetch).listVms({
    region: "us-west-2",
    provider: "aws",
    org_id: "ArkerHQ",
    public: true,
    state: "idle",
  });

  assert.equal(result.vms[0]!.max_vcpus, 8);
  assert.equal(result.vms[0]!.max_memory_mib, 32768);
  assert.equal(result.vms[0]!.min_memory_mib, 512);
  assert.deepEqual(result.vms[0]!.network, { ssh_public_keys: [] });
}

async function testForkSendsDurableFlag(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    {
      vm_id: "vm_durable",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
    },
  );

  await client(fetch).vm("source-vm-id").fork({ durable: true });

  assert.deepEqual(
    JSON.parse(fetch.calls[0]!.body!),
    { durable: true, source_vm_id: "source-vm-id" },
  );
}

async function testRunSendsIdempotencyKeyHeader(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    {
      stdout: "hi\n",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: 0,
    },
  );

  await client(fetch).vm("vm_1").run("printf hi", { idempotencyKey: "key-abc" });

  const call = fetch.calls[0]!;
  assert.equal(call.headers["idempotency-key"], "key-abc");
  assert.deepEqual(JSON.parse(call.body!), { command: "printf hi" });
}

async function testRunStatusReturnsRetryCount(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/runs/run_1",
    200,
    {
      run_id: "run_1",
      state: "completed",
      started_at: "now",
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: 0,
      retry_count: 2,
    },
  );

  const status = await client(fetch).vm("vm_1").getRun("run_1");
  assert.equal(status.retry_count, 2);
}

async function testRetryOnRetryableStatusThenSucceeds(): Promise<void> {
  const fetch = new FakeFetch();
  const matchListVms = (method: string, url: string) => method === "GET" && url === "https://arker.ai/api/v1/vms";
  // Scripted responses are one-shot and consumed in order per matching
  // predicate: first call gets the 503, the retry gets the 200.
  fetch.addJson(matchListVms, 503, RETRYABLE_ERROR_BODY);
  fetch.addJson(matchListVms, 200, { vms: [] });

  const result = await clientWithRetry(fetch, 3).listVms();

  assert.deepEqual(result.vms, []);
  assert.equal(fetch.calls.length, 2, "should have retried exactly once after the 503");
}

async function testRetryGivesUpAfterExhaustingAttempts(): Promise<void> {
  const fetch = new FakeFetch();
  const matchListVms = (method: string, url: string) => method === "GET" && url === "https://arker.ai/api/v1/vms";
  // Every attempt sees a 503 — script exactly `attempts` of them so the
  // FakeFetch throws "no scripted response" if the client ever calls one
  // extra time (an off-by-one in the retry loop).
  fetch.addJson(matchListVms, 503, RETRYABLE_ERROR_BODY);
  fetch.addJson(matchListVms, 503, RETRYABLE_ERROR_BODY);

  await assert.rejects(
    () => clientWithRetry(fetch, 2).listVms(),
    // The final attempt's parsed error code/message must surface, not a
    // generic "internal" — the caller can still branch on `unavailable`.
    (error) => error instanceof ArkerError && error.code === "unavailable" && error.status === 503,
  );
  assert.equal(fetch.calls.length, 2, "should stop retrying once attempts are exhausted, not call again");
}

async function testNonRetryableStatusFailsImmediately(): Promise<void> {
  const fetch = new FakeFetch();
  // A 400 is not in RETRYABLE_HTTP — even with attempts=3 configured, the
  // client must not burn through retries on a client error.
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://arker.ai/api/v1/vms",
    400,
    { error: { code: "invalid_request", message: "bad query", timestamp: "2026-01-01T00:00:00.000Z" } },
  );

  await assert.rejects(
    () => clientWithRetry(fetch, 3).listVms(),
    (error) => error instanceof ArkerError && error.code === "invalid_request",
  );
  assert.equal(fetch.calls.length, 1, "a non-retryable status must not be retried");
}

async function testGetAndSetPolicies(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/policies",
    200,
    { policies: [], secrets: {}, hostname: null, mitm_domains: [], warnings: [] },
  );
  const doc = await client(fetch).vm("vm_1").getPolicies();
  assert.deepEqual(doc.policies, []);

  const updated = {
    policies: [
      {
        type: "outbound" as const,
        match: { hosts: ["example.com"] },
        action: "allow" as const,
      },
    ],
  };
  fetch.addJson(
    (method, url) => method === "PUT" && url === "https://test.invalid/api/v1/vms/vm_1/policies",
    200,
    { ...updated, secrets: {}, hostname: null, mitm_domains: ["example.com"], warnings: [] },
  );
  const putResult = await client(fetch).vm("vm_1").setPolicies(updated);
  assert.equal(putResult.policies?.[0]?.type, "outbound");
  assert.deepEqual(putResult.mitm_domains, ["example.com"]);
  const putCall = fetch.calls.find((c) => c.method === "PUT");
  assert.ok(putCall, "setPolicies should PUT");
  assert.ok(putCall!.body?.includes("example.com"), "PUT body should include the policy match");
}

async function testCreateFilesystem(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/filesystems",
    200,
    {
      filesystem_id: "fs_1",
      name: "my-fs",
      owner_org_id: "ArkerHQ",
      created_at: "now",
      size_bytes: 0,
      region: "us-west-2",
      provider: "aws",
    },
  );
  const fs = await client(fetch).createFilesystem({ name: "my-fs" });
  assert.equal(fs.filesystem_id, "fs_1");
  assert.equal(fs.name, "my-fs");
  const call = fetch.calls[0]!;
  assert.equal(call.method, "POST");
  assert.ok(call.body?.includes("my-fs"));
}

async function testCancelRun(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "DELETE" && url === "https://test.invalid/api/v1/vms/vm_1/runs/run_1",
    200,
    { cancelled: true },
  );
  const result = await client(fetch).vm("vm_1").cancelRun("run_1");
  assert.equal(result.cancelled, true);
  assert.equal(fetch.calls[0]!.method, "DELETE");
}

async function testSessionCrudLifecycle(): Promise<void> {
  const fetch = new FakeFetch();
  const vm = client(fetch).vm("vm_1");

  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/sessions",
    200,
    { session_id: "sess_1", state: "idle", cwd: "/home/user", env: {} },
  );
  const created = await vm.createSession({ cwd: "/home/user" });
  assert.equal(created.session_id, "sess_1");

  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1",
    200,
    { session_id: "sess_1", state: "idle", cwd: "/home/user", env: {} },
  );
  const fetched = await vm.getSession("sess_1");
  assert.equal(fetched.session_id, "sess_1");

  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/sessions",
    200,
    { sessions: [fetched], next_cursor: null },
  );
  const listed = await vm.listSessions();
  assert.equal(listed.sessions.length, 1);
  assert.equal(listed.next_cursor, null);

  fetch.addJson(
    (method, url) => method === "PATCH" && url === "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1",
    200,
    { ok: true, session_id: "sess_1" },
  );
  const patched = await vm.updateSession("sess_1", { cols: 100, rows: 40 });
  assert.equal(patched.ok, true);
  const patchCall = fetch.calls.find((c) => c.method === "PATCH");
  assert.ok(patchCall!.body?.includes('"cols":100'));

  fetch.addJson(
    (method, url) => method === "DELETE" && url === "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1",
    200,
    { deleted: true },
  );
  const deleted = await vm.deleteSession("sess_1");
  assert.equal(deleted.deleted, true);
}

async function testConnectPtyCreatesSessionAndUsesBearerHeader(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/sessions",
    200,
    {
      session_id: "sess_1",
      vm_id: "vm_1",
      state: "idle",
      cwd: "/home/user",
      env: {},
    },
  );
  const socket = new FakeWebSocket();
  let openedUrl = "";
  let openedHeaders: Record<string, string> | undefined;
  const factory: PtyWebSocketFactory = (url, init) => {
    openedUrl = url;
    openedHeaders = init.headers;
    return socket;
  };

  const pty = await client(fetch).vm("vm_1").connectPty({
    cols: 100,
    rows: 40,
    command: "/bin/sh",
    persist: false,
    useTicket: false,
    webSocketFactory: factory,
  });

  assert.equal(pty.sessionId, "sess_1");
  assert.equal(
    openedUrl,
    "wss://test.invalid/api/v1/vms/vm_1/sessions/sess_1/pty?cols=100&rows=40&command=%2Fbin%2Fsh&persist=false",
  );
  assert.equal(openedHeaders?.authorization, "Bearer ark_live_test");
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), {});

  pty.resize(120, 33);
  pty.kill();
  pty.send("x");
  assert.equal(socket.sent[0], JSON.stringify({ type: "resize", cols: 120, rows: 33 }));
  assert.equal(socket.sent[1], JSON.stringify({ type: "kill" }));
  assert.deepEqual(socket.sent[2], new TextEncoder().encode("x"));
}

async function testConnectPtyUsesTicketForBrowserWebSocket(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1/pty-ticket",
    200,
    { ticket: "ptyt_ticket", expires_in: 300 },
  );
  let openedUrl = "";
  let openedHeaders: Record<string, string> | undefined = { unexpected: "set" };
  const factory: PtyWebSocketFactory = (url, init) => {
    openedUrl = url;
    openedHeaders = init.headers;
    return new FakeWebSocket();
  };

  await client(fetch).vm("vm_1").connectPty({
    sessionId: "sess_1",
    cols: 80,
    rows: 24,
    useTicket: true,
    webSocketFactory: factory,
  });

  assert.equal(
    openedUrl,
    "wss://test.invalid/api/v1/vms/vm_1/sessions/sess_1/pty?cols=80&rows=24&ticket=ptyt_ticket",
  );
  assert.equal(openedHeaders, undefined);
  assert.equal(fetch.calls[0]!.headers.authorization, "Bearer ark_live_test");
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), {});
}

await testForkPostsDirectlyToSourceVm();
await testForkForwardsContractFieldsItDoesNotEnumerate();
await testForkRejectsUnsupportedFlatResourceInputs();
await testForkFromImageSendsOnlyTheImage();
await testForkRejectsImageAlongsideASourceVm();
await testForkOmitsSourceOrgWhenNotExplicit();
await testForkOmitsUnconfiguredCapabilities();
await testRemovedNetworkInputsFailBeforeRequests();
await testUpdateSendsTopLevelSshPublicKeys();
await testNestedErrorWithoutOkStillParses();
await testCompletedRunDecodesOutput();
await testSyncRunPollsBackgroundedRunToCompletion();
await testRunPollBudgetIsUnboundedWithoutACallerTimeout();
await testExplicitZeroReturnsAckWithoutPolling();
await testConfiguredPlacementRoutesNamedSourcesToMainEndpoint();
testArbitraryProviderBuildsEndpoint();
testPlacementRequiresSeparateProviderAndRegion();
testInvalidProviderSyntaxFailsClosed();
await testListRegionsUsesPublicControlPlaneCatalog();
await testDiscoverRegionsRequiresNoConfiguredClient();
await testListedVmUsesItsPlacementEndpoint();
testExplicitVmHandleUsesPlacementEndpoint();
await testListRunsUsesControlPlaneAndFilters();
await testListVmsPreservesForkLimitFields();
await testForkSendsDurableFlag();
await testRunSendsIdempotencyKeyHeader();
await testRunStatusReturnsRetryCount();
async function testConnectPtyPassesCancelTtlSecs(): Promise<void> {
  // cancelTtlSecs must surface as the `cancel_ttl_secs` query param so
  // the server auto-cancels (destroys) the PTY run after that idle window.
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/sessions",
    200,
    { session_id: "sess_1", vm_id: "vm_1", state: "idle", cwd: "/home/user", env: {} },
  );
  let openedUrl = "";
  const factory: PtyWebSocketFactory = (url) => {
    openedUrl = url;
    return new FakeWebSocket();
  };
  await client(fetch).vm("vm_1").connectPty({
    cols: 80,
    rows: 24,
    cancelTtlSecs: 600,
    useTicket: false,
    webSocketFactory: factory,
  });
  assert.ok(
    openedUrl.includes("cancel_ttl_secs=600"),
    `expected cancel_ttl_secs=600 in PTY url, got: ${openedUrl}`,
  );
  // A zero/negative ttl must be omitted (no auto-cancel), not sent as 0.
  let openedUrl2 = "";
  const factory2: PtyWebSocketFactory = (url) => {
    openedUrl2 = url;
    return new FakeWebSocket();
  };
  await client(fetch).vm("vm_1").connectPty({
    sessionId: "sess_1",
    cancelTtlSecs: 0,
    useTicket: false,
    webSocketFactory: factory2,
  });
  assert.ok(
    !openedUrl2.includes("cancel_ttl_secs"),
    `cancel_ttl_secs must be omitted when <= 0, got: ${openedUrl2}`,
  );
}

async function testConnectPtyDeliversDataAndCloseEvents(): Promise<void> {
  // The PtyConnection must surface server→client bytes via onData and the
  // socket close (code/reason) via onClose.
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/sessions",
    200,
    { session_id: "sess_1", vm_id: "vm_1", state: "idle", cwd: "/home/user", env: {} },
  );
  const socket = new FakeWebSocket();
  const pty = await client(fetch).vm("vm_1").connectPty({
    useTicket: false,
    webSocketFactory: () => socket,
  });
  const received: string[] = [];
  let closed: { code?: number; reason?: string } | undefined;
  pty.onData((bytes) => received.push(new TextDecoder().decode(bytes)));
  pty.onClose((event) => { closed = event; });
  // server emits two output chunks (string + ArrayBuffer), then closes
  socket.emit("message", { data: "hello " });
  socket.emit("message", { data: new TextEncoder().encode("world").buffer });
  assert.equal(received.join(""), "hello world");
  socket.emit("close", { code: 1000, reason: "bye" });
  assert.deepEqual(closed, { code: 1000, reason: "bye" });
}

function testSurfaceStubClassificationUsesStructuredErrorCodes(): void {
  assert.equal(isExpectedSurfaceStub("not_found", "no live sync sync_does_not_exist"), true);
  assert.equal(isExpectedSurfaceStub("not_implemented", "operation unavailable"), true);
  assert.equal(isExpectedSurfaceStub("unsupported_operation", "optional feature disabled"), true);
  assert.equal(isExpectedSurfaceStub("", "request failed with HTTP 404"), true);
  assert.equal(isExpectedSurfaceStub("internal", "request failed"), false);
}

async function testRunReportsFailedWhenPlatformKilledTheRun(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    {
      run_id: "01RUN",
      state: "completed",
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: -1,
    },
  );

  const result = await client(fetch).vm("vm_1").run("sleep 30", { timeout: 2 });

  const completed = result as CompletedRunResult;
  assert.equal(completed.state, "failed");
  assert.equal(completed.exitCode, -1);
}

async function testRunKeepsCompletedForNonzeroProgramExit(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    {
      run_id: "01RUN",
      state: "completed",
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "boom\n",
      stderr_encoding: "utf-8",
      exit_code: 7,
    },
  );

  const result = await client(fetch).vm("vm_1").run("exit 7");

  const completed = result as CompletedRunResult;
  assert.equal(completed.state, "completed");
  assert.equal(completed.exitCode, 7);
}

async function testRunAndGetRunAgreeOnStateForKilledRun(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_1/runs",
    200,
    {
      run_id: "01RUN",
      state: "completed",
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: -1,
    },
  );
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://test.invalid/api/v1/vms/vm_1/runs/01RUN",
    200,
    {
      run_id: "01RUN",
      state: "failed",
      started_at: "2026-07-27T00:00:00Z",
      exit_code: null,
      fail_reason: "the compute environment became unavailable",
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
    },
  );

  const vm = client(fetch).vm("vm_1");
  const sync = (await vm.run("sleep 30", { timeout: 2 })) as CompletedRunResult;
  const stored = await vm.getRun("01RUN");

  assert.equal(sync.state, "failed");
  assert.equal(stored.state, "failed");
}

// ── Binary output ──────────────────────────────────────────────────
// A run can emit anything: an image, an archive, random bytes. The text view is
// lossy for those by definition, so `*Bytes` must round-trip them exactly.

// 1x1 PNG. Starts with 0x89, not a valid UTF-8 start byte, so decoding to text
// mangles it — which is the whole point of keeping the bytes.
const PNG_1X1 = Uint8Array.from(
  atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="),
  (c) => c.charCodeAt(0),
);

function binaryRunBody(payload: Uint8Array): Record<string, unknown> {
  let binary = "";
  for (const byte of payload) binary += String.fromCharCode(byte);
  return {
    stdout: btoa(binary),
    stdout_encoding: "base64",
    stderr: "",
    stderr_encoding: "utf-8",
    exit_code: 0,
  };
}

async function testRunReturnsImageBytesIntactAndTextIsLossy(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson((m, u) => m === "POST" && u.endsWith("/v1/vms/vm_1/runs"), 200, binaryRunBody(PNG_1X1));

  const result = (await client(fetch).vm("vm_1").run("cat photo.png")) as CompletedRunResult;

  // The bytes survive exactly — you can write them straight to a file.
  assert.deepEqual(result.stdoutBytes, PNG_1X1);
  assert.deepEqual(result.stdoutBytes.slice(0, 8), Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  // The text view is lossy for binary, and must not throw.
  assert.equal(typeof result.stdout, "string");
  assert.ok(result.stdout.includes("\uFFFD"));
}

async function testGetRunReturnsImageBytesIntact(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson((m, u) => m === "GET" && u.endsWith("/runs/run_1"), 200, {
    ...binaryRunBody(PNG_1X1),
    run_id: "run_1",
    state: "completed",
    started_at: "now",
  });

  const stored = await client(fetch).vm("vm_1").getRun("run_1");

  assert.deepEqual(stored.stdoutBytes, PNG_1X1);
  assert.ok(stored.stdout.includes("\uFFFD"));
}

async function testRunAndGetRunAgreeOnBinaryOutput(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson((m, u) => m === "POST" && u.endsWith("/v1/vms/vm_1/runs"), 200, binaryRunBody(PNG_1X1));
  fetch.addJson((m, u) => m === "GET" && u.endsWith("/runs/run_1"), 200, {
    ...binaryRunBody(PNG_1X1),
    run_id: "run_1",
    state: "completed",
    started_at: "now",
  });

  const vm = client(fetch).vm("vm_1");
  const live = (await vm.run("cat photo.png")) as CompletedRunResult;
  const stored = await vm.getRun("run_1");

  assert.deepEqual(live.stdoutBytes, stored.stdoutBytes);
  assert.equal(live.stdout, stored.stdout);
}

async function testEveryByteValueRoundTrips(): Promise<void> {
  const payload = Uint8Array.from({ length: 256 }, (_, i) => i);
  const fetch = new FakeFetch();
  fetch.addJson((m, u) => m === "POST" && u.endsWith("/v1/vms/vm_1/runs"), 200, binaryRunBody(payload));

  const result = (await client(fetch).vm("vm_1").run("cat /dev/urandom")) as CompletedRunResult;

  assert.deepEqual(result.stdoutBytes, payload);
  assert.equal(result.stdoutBytes.length, 256);
  // Lossy as text: distinct bytes collapse onto the replacement char.
  assert.ok(new Set(result.stdout).size < 256);
}

async function testUtf8WireStillYieldsBothForms(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson((m, u) => m === "POST" && u.endsWith("/v1/vms/vm_1/runs"), 200, {
    stdout: "caf\u00e9 \u{1f389}\n",
    stdout_encoding: "utf-8",
    stderr: "",
    stderr_encoding: "utf-8",
    exit_code: 0,
  });

  const result = (await client(fetch).vm("vm_1").run("echo cafe")) as CompletedRunResult;

  assert.equal(result.stdout, "caf\u00e9 \u{1f389}\n");
  assert.deepEqual(result.stdoutBytes, new TextEncoder().encode("caf\u00e9 \u{1f389}\n"));
  assert.equal(new TextDecoder().decode(result.stdoutBytes), result.stdout);
}


await testConnectPtyCreatesSessionAndUsesBearerHeader();
await testConnectPtyUsesTicketForBrowserWebSocket();
await testConnectPtyPassesCancelTtlSecs();
await testConnectPtyDeliversDataAndCloseEvents();
testSurfaceStubClassificationUsesStructuredErrorCodes();
await testRetryOnRetryableStatusThenSucceeds();
await testRetryGivesUpAfterExhaustingAttempts();
await testNonRetryableStatusFailsImmediately();
await testGetAndSetPolicies();
await testCreateFilesystem();
await testCancelRun();
await testSessionCrudLifecycle();
await testRunReportsFailedWhenPlatformKilledTheRun();
await testRunKeepsCompletedForNonzeroProgramExit();
await testRunAndGetRunAgreeOnStateForKilledRun();

await testRunReturnsImageBytesIntactAndTextIsLossy();
await testGetRunReturnsImageBytesIntact();
async function testRetryHonoursServerRetryAfter(): Promise<void> {
  // A hint beats backoff when the caller left maxDelayMs at its default: the
  // default exists to shape backoff, and capping the hint with it would
  // neuter real capacity waits.
  const client = new Arker({ apiKey: "k", baseUrl: "http://x", retry: { attempts: 4, baseDelayMs: 200, jitterMs: 0 } });
  assert.equal(client._retryDelay(0, { code: "unavailable", message: "", retryAfterS: 30 }), 30_000);

  // Without a hint the existing backoff is untouched.
  assert.equal(client._retryDelay(0), 200);
  assert.equal(client._retryDelay(2), 800);
  assert.equal(client._retryDelay(1, { code: "unavailable", message: "" }), 400);

  // An explicitly configured maxDelayMs is the caller's latency budget, and
  // it caps the hint too.
  const capped = new Arker({ apiKey: "k", baseUrl: "http://x", retry: { attempts: 4, baseDelayMs: 200, maxDelayMs: 2_000, jitterMs: 0 } });
  assert.equal(capped._retryDelay(0, { code: "unavailable", message: "", retryAfterS: 30 }), 2_000);
}

async function testRetryAfterHintDrivesTheActualSleep(): Promise<void> {
  // The one test of the wiring: the request loop must hand the parsed error
  // to retryDelay, or the hint silently never applies. Lower bound only.
  const fetchImpl = new FakeFetch();
  fetchImpl.addJson((m, u) => m === "POST" && u.includes("/fork"), 503, {
    error: { code: "unavailable", message: "cold", retry_after: 0.05, timestamp: new Date().toISOString() },
  });
  fetchImpl.addJson((m, u) => m === "POST" && u.includes("/fork"), 200, { vm_id: "vm-1", state: "running" });
  const client = new Arker({
    apiKey: "k",
    baseUrl: "http://x",
    fetch: fetchImpl.fetch,
    retry: { attempts: 2, baseDelayMs: 1, jitterMs: 0 },
  });
  const started = Date.now();
  const vm = await client.fork("source-vm");
  assert.equal(vm.id, "vm-1");
  assert.ok(Date.now() - started >= 45, "the 50ms hint must drive the sleep");
  assert.equal(fetchImpl.calls.length, 2);
}

await testRetryHonoursServerRetryAfter();
await testRetryAfterHintDrivesTheActualSleep();

function unavailableBody(retryAfterS: number): unknown {
  return {
    error: {
      code: "unavailable",
      message: "at capacity",
      retry_after: retryAfterS,
      timestamp: new Date().toISOString(),
    },
  };
}

async function testQueueingTimeoutRetriesPastTheAttemptCap(): Promise<void> {
  // The window is the budget: three failures exceed attempts=2, still succeeds.
  const runs = (m: string, u: string) => m === "POST" && u.includes("/runs");
  const fetchImpl = new FakeFetch();
  fetchImpl.addJson(runs, 503, unavailableBody(0.05));
  fetchImpl.addJson(runs, 503, unavailableBody(0.05));
  fetchImpl.addJson(runs, 503, unavailableBody(0.05));
  fetchImpl.addJson(runs, 200, { run_id: "run_q", state: "completed", exit_code: 0, stdout: "ok", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8" });
  const client = new Arker({
    apiKey: "k",
    baseUrl: "http://x",
    fetch: fetchImpl.fetch,
    retry: { attempts: 2, baseDelayMs: 1, jitterMs: 0 },
  });
  const vm = client.vm("vm-1");
  const result = await vm.run("echo ok", { queueing_timeout: 30 });
  assert.equal(result.type, "completed");
  assert.equal(fetchImpl.calls.length, 4, "the window must outlast attempts=2");
}

async function testQueueingWindowDrainsThenSurfacesUnavailable(): Promise<void> {
  // 3s window, 1.1s hints: bodies re-send the remaining window (3, 2, 1),
  // then the error surfaces without sleeping past the deadline.
  const runs = (m: string, u: string) => m === "POST" && u.includes("/runs");
  const fetchImpl = new FakeFetch();
  fetchImpl.addJson(runs, 503, unavailableBody(1.1));
  fetchImpl.addJson(runs, 503, unavailableBody(1.1));
  fetchImpl.addJson(runs, 503, unavailableBody(1.1));
  const client = new Arker({
    apiKey: "k",
    baseUrl: "http://x",
    fetch: fetchImpl.fetch,
    retry: { attempts: 4, baseDelayMs: 1, jitterMs: 0 },
  });
  const started = Date.now();
  await assert.rejects(
    client.vm("vm-1").run("true", { queueing_timeout: 3 }),
    (error: unknown) => error instanceof ArkerError && error.code === "unavailable" && error.status === 503,
  );
  const elapsed = Date.now() - started;
  const sent = fetchImpl.calls.map(
    (call) => (JSON.parse(call.body ?? "{}") as { queueing_timeout?: number }).queueing_timeout,
  );
  assert.deepEqual(sent, [3, 2, 1]);
  assert.ok(elapsed >= 2_000 && elapsed < 5_000, `waited ${elapsed}ms`);
}

async function testQueueingTimeoutRespectsRetryFalse(): Promise<void> {
  // retry: false = exactly one request, window or not.
  const runs = (m: string, u: string) => m === "POST" && u.includes("/runs");
  const fetchImpl = new FakeFetch();
  fetchImpl.addJson(runs, 503, unavailableBody(0.05));
  const client = new Arker({
    apiKey: "k",
    baseUrl: "http://x",
    fetch: fetchImpl.fetch,
    retry: false,
  });
  await assert.rejects(
    client.vm("vm-1").run("true", { queueing_timeout: 30 }),
    (error: unknown) => error instanceof ArkerError && error.code === "unavailable",
  );
  assert.equal(fetchImpl.calls.length, 1);
}

async function testForkForwardsQueueingTimeout(): Promise<void> {
  // fork() builds its body from an explicit allowlist — pin that the field
  // survives it. The retry mechanics are shared with run and tested there.
  const fetchImpl = new FakeFetch();
  fetchImpl.addJson((m, u) => m === "POST" && u.includes("/fork"), 200, { vm_id: "vm-9", state: "running" });
  const client = new Arker({ apiKey: "k", baseUrl: "http://x", fetch: fetchImpl.fetch, retry: false });
  await client.fork("arkuntu", { queueing_timeout: 30 });
  const body = JSON.parse(fetchImpl.calls[0]!.body ?? "{}") as { queueing_timeout?: number };
  assert.equal(body.queueing_timeout, 30);
}

await testQueueingTimeoutRetriesPastTheAttemptCap();
await testQueueingWindowDrainsThenSurfacesUnavailable();
await testQueueingTimeoutRespectsRetryFalse();
await testForkForwardsQueueingTimeout();
await testRunAndGetRunAgreeOnBinaryOutput();
await testEveryByteValueRoundTrips();
await testUtf8WireStillYieldsBothForms();

// ── Directory upload: compression ────────────────────────────────────
// A whole tree is packed into ONE archive and uploaded in a single request, so
// on a payload that genuinely compresses, compressing it is what keeps a large
// tree inside the accepted request size at all — not merely an optimization.

async function testSyncDirUploadsAGzippedTarball(): Promise<void> {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const nodePath = await import("node:path");
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "syncdir-gz-"));
  for (let i = 0; i < 40; i++) {
    fs.writeFileSync(nodePath.join(dir, `f${i}.ts`), `export const v${i} = ${i};\n`.repeat(60));
  }

  const fetch = new FakeFetch();
  // 1. remote manifest -> empty, so every file counts as changed
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, { ok: true, op: "manifest", entries: [] });
  // 2. raw /sync unsupported -> 404 -> legacy upload+run path below
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 404, {
    error: { code: "not_found", message: "no route" },
  });
  // 3. the tarball write (small enough to go inline)
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
    ok: true, op: "write",
    results: [{ path: "/tmp/t.tar.gz", complete: true, written: true, ranges: [], received: 0 }],
  });
  // 4. the in-guest extract
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/runs"), 200, {
    run_id: "r1", state: "completed", exit_code: 0, stdout: "", stderr: "",
    stdout_encoding: "utf-8", stderr_encoding: "utf-8",
  });

  await client(fetch).vm("vm_1").syncDir(dir, "/home/user/p");

  const write = fetch.calls.find(
    (c) => c.url.endsWith("/sync") && (c.body ?? "").includes('"op":"write"'),
  );
  assert.ok(write, "syncDir must upload the tarball");
  const entry = JSON.parse(write!.body!).writes[0];
  const tarball = Buffer.from(entry.content, "base64");
  assert.equal(tarball[0], 0x1f, "tarball must be gzipped (magic byte 0)");
  assert.equal(tarball[1], 0x8b, "tarball must be gzipped (magic byte 1)");
  fs.rmSync(dir, { recursive: true, force: true });
}

await testSyncDirUploadsAGzippedTarball();

// A >20MB inline-fallback write is the exact shape that hard-failed with
// `payload_too_large` before arkerd's `MAX_SYNC_INLINE_REQUEST_BYTES` cap was
// respected client-side: `syncWriteInline` used to put the WHOLE file — not
// even chunked — in ONE request's ONE entry regardless of size. It must now
// split into multiple sequential requests, each staying within
// `INLINE_WRITE_LIMIT`, sharing one `upload_id` so arkerd's
// `SyncChunkSessionState` ledger reassembles them across requests.
async function testLargeInlineWriteSplitsAcrossMultipleRequests(): Promise<void> {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const nodePath = await import("node:path");
  const crypto = await import("node:crypto");
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "syncdir-inline-big-"));
  // Random bytes so gzip cannot shrink the tarball below the size that
  // forces multiple inline requests (24MiB: > INLINE_WRITE_LIMIT=16MiB and >
  // arkerd's 20MiB MAX_SYNC_INLINE_REQUEST_BYTES).
  const fileSize = 24 * 1024 * 1024;
  fs.writeFileSync(nodePath.join(dir, "blob.bin"), crypto.randomBytes(fileSize));

  const fetch = new FakeFetch();
  // 1. remote manifest -> empty, so the file counts as changed
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, { ok: true, op: "manifest", entries: [] });
  // 2. raw /sync unsupported -> 404 -> legacy inline-write fallback
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 404, {
    error: { code: "not_found", message: "no route" },
  });
  // 3. every inline write request (however many it takes): echo back
  // completion based on whether THIS entry's range reaches the declared
  // size, same contract arkerd's chunk-session ledger implements. Sticky, so
  // it answers every batch, not just the first.
  fetch.addDynamicJson(
    (m, url) => m === "POST" && url.endsWith("/sync"),
    (call) => {
      const parsed = JSON.parse(call.body ?? "{}") as {
        writes?: Array<{ path: string; size: number; end: number }>;
      };
      const writes = parsed.writes ?? [];
      const results = writes.map((w) => {
        const complete = w.end === w.size;
        return {
          path: w.path,
          size: w.size,
          received_bytes: w.end,
          ranges: [{ start: 0, end: w.end }],
          complete,
          written: complete,
        };
      });
      return { status: 200, body: { ok: true, op: "write", results } };
    },
  );
  // 4. the in-guest extract
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/runs"), 200, {
    run_id: "r1", state: "completed", exit_code: 0, stdout: "", stderr: "",
    stdout_encoding: "utf-8", stderr_encoding: "utf-8",
  });

  await client(fetch).vm("vm_1").syncDir(dir, "/home/user/p");

  const writeCalls = fetch.calls.filter(
    (c) => c.url.endsWith("/sync") && (c.body ?? "").includes('"op":"write"'),
  );
  // The fix's whole point: this is NOT one request carrying the entire file.
  assert.ok(writeCalls.length >= 2, `expected multiple inline write requests, got ${writeCalls.length}`);

  const allWrites: Array<{ upload_id: string; size: number; start: number; end: number; content: string }> = [];
  for (const call of writeCalls) {
    const writes = JSON.parse(call.body!).writes as Array<{
      upload_id: string; size: number; start: number; end: number; content: string;
    }>;
    allWrites.push(...writes);
    const decodedTotal = writes.reduce((sum, w) => sum + Buffer.from(w.content, "base64").length, 0);
    // Each request's own decoded total must stay within the server's inline
    // cap. Before the fix, one request carried the whole (>20MB) file and
    // arkerd rejected it with `payload_too_large`.
    assert.ok(
      decodedTotal <= 16 * 1024 * 1024,
      `one inline request carried ${decodedTotal} decoded bytes, over INLINE_WRITE_LIMIT`,
    );
  }

  // Every chunk shares one upload_id, so arkerd's cross-request
  // chunk-session ledger treats the split requests as one upload.
  const uploadIds = new Set(allWrites.map((w) => w.upload_id));
  assert.equal(uploadIds.size, 1, "all chunks of one file must share an upload_id");

  // Chunks reassemble losslessly, in order, with no gaps or overlaps, and
  // cover the whole declared file size.
  const reconstructed = Buffer.concat(allWrites.map((w) => Buffer.from(w.content, "base64")));
  assert.equal(reconstructed.length, allWrites[0]!.size, "chunks must cover [0, size) exactly");

  fs.rmSync(dir, { recursive: true, force: true });
}

await testLargeInlineWriteSplitsAcrossMultipleRequests();

// ── Directory upload: the preferred single-request path ──────────────
// A whole tree travels as one archive in one request; the compatibility
// fallback below is only reached when a deployment does not offer it.

/** A syncDir fixture: a temp dir of `n` small files, plus a scripted manifest. */
async function syncDirFixture(n = 8) {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const nodePath = await import("node:path");
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "syncdir-stream-"));
  for (let i = 0; i < n; i++) {
    fs.writeFileSync(nodePath.join(dir, `f${i}.ts`), `export const v${i} = ${i};\n`);
  }
  const fetch = new FakeFetch();
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, { ok: true, op: "manifest", entries: [] });
  const cleanup = () => fs.rmSync(dir, { recursive: true, force: true });
  return { dir, fetch, cleanup };
}

async function testSyncStreamFastPathSkipsTheExtractRun(): Promise<void> {
  const { dir, fetch, cleanup } = await syncDirFixture();
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });

  await client(fetch).vm("vm_1").syncDir(dir, "/home/user/p");

  const stream = fetch.calls.find((c) => c.url.includes("/sync?"));
  assert.ok(stream, "syncDir must use raw /sync");
  assert.equal(stream!.headers["content-type"], "application/octet-stream", "body must go raw, not base64 JSON");

  // Params ride in the query string, not as headers, so they reach the VM
  // reliably regardless of what strips custom headers along the way.
  const qs = new URL(stream!.url).searchParams;
  assert.equal(qs.get("path"), "/home/user/p");
  assert.equal(qs.get("extract"), "tar.gz");
  assert.ok(Number(qs.get("size")) > 0, "size must be the tarball byte length");

  // The whole point: no second round-trip to run the extraction as a command.
  assert.equal(fetch.calls.filter((c) => c.url.endsWith("/runs")).length, 0, "fast path must not issue an extract run");
  cleanup();
}

async function testSyncStreamErrorsOtherThan404DoNotFallBack(): Promise<void> {
  const { dir, fetch, cleanup } = await syncDirFixture();
  // A path escape is a REAL rejection. Silently retrying the slow path would
  // turn a hard error into a confusing one.
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 403, {
    error: { code: "permission_denied", message: "path escapes the VM root" },
  });

  await assert.rejects(
    () => client(fetch).vm("vm_1").syncDir(dir, "/home/user/p"),
    (error: unknown) => {
      assert.ok(error instanceof ArkerError, "must surface as ArkerError");
      assert.equal((error as ArkerError).code, "permission_denied", "server's code must survive");
      return true;
    },
  );
  assert.equal(fetch.calls.filter((c) => c.url.endsWith("/runs")).length, 0, "must not fall back on a real failure");
  cleanup();
}

await testSyncStreamFastPathSkipsTheExtractRun();
await testSyncStreamErrorsOtherThan404DoNotFallBack();

// A changed set that would exceed arkerd's per-request MAX_SYNC_FILE_BYTES
// cap (2 GiB, aws/arkerd-worker-linux/src/api/routes/sync.rs) used to be
// packed into ONE archive regardless of total size: a cold sync of a
// tens-of-GB tree, or a warm sync that happened to touch that much, failed
// outright with 413 payload_too_large instead of merely being slow. It must
// now split into multiple archives. Real fixture files stay tiny (this is a
// unit test); `archiveBatchMaxBytes` overrides the real ~1.5 GB default so
// the same batching code path is exercised at a size the test can afford.
async function testSyncDirSplitsOversizedChangedSet(): Promise<void> {
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "syncdir-batch-"));
  for (const name of ["a.txt", "b.txt", "c.txt"]) {
    fs.writeFileSync(nodePath.join(dir, name), "12345"); // 5 bytes == the overridden cap
  }

  const fetch = new FakeFetch();
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, { ok: true, op: "manifest", entries: [] });
  // Sticky: answers every raw /sync call, however many there are.
  fetch.addDynamicJson((m, url) => m === "POST" && url.includes("/sync?"), () => ({
    status: 200,
    body: { ok: true },
  }));

  const result = await client(fetch).vm("vm_1").syncDir(dir, "/home/user/p", { archiveBatchMaxBytes: 5 });

  const streamCalls = fetch.calls.filter((c) => c.url.includes("/sync?"));
  // One 5-byte file per batch under a 5-byte cap: three files, three
  // archives -- never one archive carrying all three.
  assert.equal(result.sent, 3);
  assert.equal(streamCalls.length, 3, `expected 3 archive batches, got ${streamCalls.length}`);
  for (const call of streamCalls) {
    const extract = new URL(call.url).searchParams.get("extract");
    assert.ok(extract === "tar" || extract === "tar.gz", `expected an archive extract, got ${extract}`);
  }

  fs.rmSync(dir, { recursive: true, force: true });
}

await testSyncDirSplitsOversizedChangedSet();

// ── syncDir assumeEmpty ──────────────────────────────────────────────
// The manifest exists to avoid re-sending unchanged files. Into a fresh
// directory it is guaranteed empty, so the round-trip costs real latency to
// learn nothing — on exactly the first-sync path where every millisecond
// matters most.

async function testAssumeEmptySkipsTheManifestRoundTrip(): Promise<void> {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const nodePath = await import("node:path");
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "syncdir-assume-"));
  fs.writeFileSync(nodePath.join(dir, "a.ts"), "export const a = 1;\n");

  const fetch = new FakeFetch();
  // Deliberately NO manifest script: if syncDir asks for one, FakeFetch throws.
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });

  const result = await client(fetch).vm("vm_1").syncDir(dir, "/home/user/p", { assumeEmpty: true });

  assert.equal(
    fetch.calls.filter((c) => (c.body ?? "").includes('"op":"manifest"')).length,
    0,
    "assumeEmpty must not fetch the remote manifest",
  );
  assert.equal(result.sent, 1, "an empty manifest means everything is sent");
  // Exactly 0, not merely small, so a caller can tell "skipped" from "fast".
  assert.equal(result.timings?.manifestMs, 0, "a skipped manifest must report exactly 0");
  fs.rmSync(dir, { recursive: true, force: true });
}

await testAssumeEmptySkipsTheManifestRoundTrip();

console.log("PASS unit");

// ── syncDir stat cache ───────────────────────────────────────────────────────
// The cache decides whether a file is re-read at all, so a wrong "unchanged"
// answer means a silently skipped upload. These tests pin that boundary.

/** A server that answers a syncDir: empty manifest, then accepts the tarball. */
function syncDirServer(remoteEntries: Array<{ path: string; hash: string }> = []): FakeFetch {
  const fetch = new FakeFetch();
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
    ok: true, op: "manifest", entries: remoteEntries, truncated: false,
  });
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });
  return fetch;
}

function tmpTree(files: Record<string, string>): string {
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-statcache-"));
  for (const [name, body] of Object.entries(files)) {
    fs.mkdirSync(nodePath.dirname(nodePath.join(dir, name)), { recursive: true });
    fs.writeFileSync(nodePath.join(dir, name), body);
  }
  return dir;
}

/** Isolate the persisted cache per test so they cannot bleed into each other. */
function withCacheDir<T>(fn: (cacheDir: string) => Promise<T>): Promise<T> {
  const cacheDir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-cachedir-"));
  const previous = process.env.ARKER_CACHE_DIR;
  process.env.ARKER_CACHE_DIR = cacheDir;
  return fn(cacheDir).finally(() => {
    if (previous === undefined) delete process.env.ARKER_CACHE_DIR;
    else process.env.ARKER_CACHE_DIR = previous;
    fs.rmSync(cacheDir, { recursive: true, force: true });
  });
}

async function testStatCacheSkipsRereadOnSecondSync(): Promise<void> {
  await withCacheDir(async () => {
    const dir = tmpTree({ "a.txt": "alpha", "b.txt": "bravo" });
    const first = await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");
    assert.equal(first.sent, 2, "first sync uploads everything");

    // Second run: the remote now reports the same hashes, and nothing local
    // changed, so every file must be skipped.
    const hashes = ["a.txt", "b.txt"].map((rel) => ({
      path: rel,
      hash: createHash("sha256").update(fs.readFileSync(nodePath.join(dir, rel))).digest("hex"),
    }));
    const second = await client(syncDirServer(hashes)).vm("vm_1").syncDir(dir, "/p");
    assert.equal(second.sent, 0, "unchanged tree must send nothing");
    assert.equal(second.skipped, 2);
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testStatCacheCatchesForgedMtimeEdit(): Promise<void> {
  // THE regression this cache design exists for. A (size, mtime) key can be
  // defeated by anything that restores timestamps — cp -p, GNU tar -x, touch -r
  // — and the changed file would be silently NOT uploaded.
  //
  // Node's utimesSync truncates sub-millisecond precision, so it cannot forge
  // an mtime exactly (real tools can). Rather than depend on that, this forges
  // the recorded signature directly: rewrite the cached entry so size AND mtime
  // match the edited file exactly, leaving only ctime stale. That is precisely
  // the state a timestamp-restoring tool produces, and precisely what the
  // narrow key cannot see.
  await withCacheDir(async (cacheDir) => {
    const dir = tmpTree({ "a.txt": "AAAAA" });
    const abs = nodePath.join(dir, "a.txt");

    const first = await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");
    assert.equal(first.sent, 1);

    // The test needs the post-edit ctime to DIFFER from the recorded one, and
    // ctime granularity is filesystem-dependent (often 1ms). Rewrite until the
    // clock has actually moved on rather than assuming it has.
    const before = String(fs.statSync(abs, { bigint: true }).ctimeNs);
    let now = fs.statSync(abs, { bigint: true });
    for (let attempt = 0; attempt < 100; attempt++) {
      fs.writeFileSync(abs, "BBBBB"); // same length, different content
      now = fs.statSync(abs, { bigint: true });
      if (String(now.ctimeNs) !== before) break;
      await sleep(2);
    }

    const sub = nodePath.join(cacheDir, "arker", "syncdir");
    const cacheFile = nodePath.join(sub, fs.readdirSync(sub)[0]!);
    const parsed = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
    const entry = parsed.entries["a.txt"];
    // Perfect (size, mtime) forge; ctime deliberately left at the old value.
    entry.size = Number(now.size);
    entry.mtimeNs = String(now.mtimeNs);
    entry.ino = String(now.ino);
    entry.dev = String(now.dev);
    entry.mode = Number(now.mode);
    assert.notEqual(entry.ctimeNs, String(now.ctimeNs), "ctime must differ after a write");
    // Keep it outside the racily-clean window so THIS is what forces the re-hash.
    parsed.writtenNs = String(BigInt(entry.mtimeNs) + 10_000_000_000n);
    fs.writeFileSync(cacheFile, JSON.stringify(parsed));

    // Remote still holds the ORIGINAL content's hash. A cache trusting
    // (size, mtime) would match it and skip — losing the edit.
    const staleHash = createHash("sha256").update("AAAAA").digest("hex");
    const second = await client(syncDirServer([{ path: "a.txt", hash: staleHash }]))
      .vm("vm_1").syncDir(dir, "/p");
    assert.equal(second.sent, 1, "a forged-mtime edit must still be uploaded");
    assert.equal(second.skipped, 0);
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testStatCacheDistrustsRacilyCleanEntries(): Promise<void> {
  // A file written in the same instant as the cache cannot be distinguished
  // from one written just after it, so it must be re-hashed rather than
  // trusted. Simulated by backdating the cache's own write stamp.
  await withCacheDir(async (cacheDir) => {
    const dir = tmpTree({ "a.txt": "alpha" });
    await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");

    const files = fs.readdirSync(nodePath.join(cacheDir, "arker", "syncdir"));
    assert.equal(files.length, 1, "a cache file must have been written");
    const cacheFile = nodePath.join(cacheDir, "arker", "syncdir", files[0]!);
    const parsed = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
    const entry = Object.values(parsed.entries)[0] as { mtimeNs: string };
    // Claim the cache was written at the same moment the file was modified.
    parsed.writtenNs = entry.mtimeNs;
    fs.writeFileSync(cacheFile, JSON.stringify(parsed));

    const hash = createHash("sha256").update("alpha").digest("hex");
    const again = await client(syncDirServer([{ path: "a.txt", hash }])).vm("vm_1").syncDir(dir, "/p");
    // Re-hashed (not trusted from stat) — and since the content really is
    // unchanged, the recomputed hash matches the remote and it is still skipped.
    assert.equal(again.sent, 0, "racily-clean entry re-hashes to the same value");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testStatCacheSurvivesCorruptionAndBadVersion(): Promise<void> {
  for (const contents of ["not json at all", JSON.stringify({ version: 999, entries: {} }), ""]) {
    await withCacheDir(async (cacheDir) => {
      const dir = tmpTree({ "a.txt": "alpha" });
      await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");
      const sub = nodePath.join(cacheDir, "arker", "syncdir");
      const cacheFile = nodePath.join(sub, fs.readdirSync(sub)[0]!);
      fs.writeFileSync(cacheFile, contents);

      // Must fall back to hashing rather than throwing.
      const again = await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");
      assert.equal(again.sent, 1, `corrupt cache (${contents.slice(0, 12)}) must not break sync`);
      fs.rmSync(dir, { recursive: true, force: true });
    });
  }
}

async function testStatCacheIsBestEffortWhenUnwritable(): Promise<void> {
  // A cache must never be able to fail a sync. Point it at a path that cannot
  // be created (a FILE where a directory must go).
  const blocker = nodePath.join(fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-blk-")), "notadir");
  fs.writeFileSync(blocker, "");
  const previous = process.env.ARKER_CACHE_DIR;
  process.env.ARKER_CACHE_DIR = blocker;
  try {
    const dir = tmpTree({ "a.txt": "alpha" });
    const res = await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");
    assert.equal(res.sent, 1, "sync must succeed even when the cache cannot be written");
    fs.rmSync(dir, { recursive: true, force: true });
  } finally {
    if (previous === undefined) delete process.env.ARKER_CACHE_DIR;
    else process.env.ARKER_CACHE_DIR = previous;
  }
}

async function testStatCacheNotWrittenWhenUploadFails(): Promise<void> {
  // Persisting before the upload lands would record files as synced that never
  // arrived — every later run would then skip them.
  await withCacheDir(async (cacheDir) => {
    const dir = tmpTree({ "a.txt": "alpha" });
    const fetch = new FakeFetch();
    fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
      ok: true, op: "manifest", entries: [], truncated: false,
    });
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 500, {
      error: { code: "internal", message: "nope" },
    });
    await assert.rejects(() => client(fetch).vm("vm_1").syncDir(dir, "/p"));

    const sub = nodePath.join(cacheDir, "arker", "syncdir");
    const wrote = fs.existsSync(sub) && fs.readdirSync(sub).length > 0;
    assert.equal(wrote, false, "a failed upload must not leave a cache entry behind");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testCallerSuppliedCacheBypassesDisk(): Promise<void> {
  await withCacheDir(async (cacheDir) => {
    const dir = tmpTree({ "a.txt": "alpha" });
    await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p", { cache: new Map() });
    const sub = nodePath.join(cacheDir, "arker", "syncdir");
    assert.equal(fs.existsSync(sub) && fs.readdirSync(sub).length > 0, false,
      "an explicit cache must keep the SDK out of the user's cache dir");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

await testStatCacheSkipsRereadOnSecondSync();
await testStatCacheCatchesForgedMtimeEdit();
await testStatCacheDistrustsRacilyCleanEntries();
await testStatCacheSurvivesCorruptionAndBadVersion();
await testStatCacheIsBestEffortWhenUnwritable();
await testStatCacheNotWrittenWhenUploadFails();
await testCallerSuppliedCacheBypassesDisk();

// ── run() wait contract ──────────────────────────────────────────────────────
// Mirrors the Python suite. `timeout` is a SERVER-side kill bound; omitting it
// means the run is unbounded, so this client must not impose a deadline of its
// own. What still ends an unbounded wait is the SERVICE going unreachable.
//
// These drive real polling loops, so they run on a virtual clock: setTimeout
// fires immediately while Date.now() advances by the requested delay. Without
// it, asserting "still waiting after 3600s" would take an hour.
async function withFakeClock<T>(fn: () => Promise<T>): Promise<T> {
  const realSetTimeout = globalThis.setTimeout;
  const realNow = Date.now;
  let now = realNow();
  (globalThis as unknown as { setTimeout: unknown }).setTimeout = ((cb: () => void, ms?: number) => {
    now += ms ?? 0;
    return realSetTimeout(cb, 0);
  }) as unknown as typeof globalThis.setTimeout;
  Date.now = () => now;
  try {
    return await fn();
  } finally {
    globalThis.setTimeout = realSetTimeout;
    Date.now = realNow;
  }
}

// A COMPLETE error envelope. `timestamp` is not decoration: without it the
// SDK cannot parse the wire code and reports `internal`, so a fixture missing
// it would silently exercise a path the real service never produces.
const UNAVAILABLE_ENVELOPE = { error: { code: "unavailable", message: "slot busy", timestamp: "2026-08-21T00:00:00.000Z", retry_after: 1, retryable: true } };
const RUNNING_RUN = { run_id: "run_bg", state: "running", started_at: "now", exit_code: null, stdout: "", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8" };
const COMPLETED_RUN = { run_id: "run_bg", state: "completed", started_at: "now", exit_code: 0, stdout: "done\n", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8" };

/** A fetch whose Nth poll response is decided by `plan` — no per-call scripting. */
function pollingFetch(plan: (pollIndex: number) => { status: number; body: unknown }) {
  let polls = 0;
  const state = {
    posts: 0,
    get polls() { return polls; },
    fetch: (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const method = init?.method ?? "GET";
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (method === "POST") {
        state.posts += 1;
        return json(200, { run_id: "run_bg", state: "running" });
      }
      const { status, body } = plan(polls++);
      return json(status, body);
    }) as unknown as typeof globalThis.fetch,
  };
  return state;
}

function pollingClient(fetch: typeof globalThis.fetch): Arker {
  return new Arker({ apiKey: "ark_live_test", baseUrl: "https://test.invalid/api/", fetch, retry: false });
}

async function testUnsetTimeoutNeverGivesUpOnAStillRunningRun(): Promise<void> {
  // THE regression assertion. The old client capped an unset timeout at a
  // 3600s CLIENT-side budget and threw "timeout" on a run that was perfectly
  // healthy. 1500 polls at the 3s cap is >4500s of virtual time — comfortably
  // past that old budget — and it must still resolve.
  const stub = pollingFetch((n) => ({ status: 200, body: n < 1500 ? RUNNING_RUN : COMPLETED_RUN }));
  const result = await withFakeClock(() => pollingClient(stub.fetch).vm("vm_1").run("sleep forever"));
  assert.equal(result.type, "completed");
  assert.equal((result as CompletedRunResult).exitCode, 0);
  assert.ok(stub.polls > 1500, `expected to poll past the old 3600s budget, polled ${stub.polls}`);
}

async function testExplicitTimeoutStillBoundsTheWait(): Promise<void> {
  // Opting IN to a bound must still work: timeout=2 gives a 2s kill bound plus
  // the 30s margin, and a run that never terminates has to throw "timeout".
  const stub = pollingFetch(() => ({ status: 200, body: RUNNING_RUN }));
  await assert.rejects(
    withFakeClock(() => pollingClient(stub.fetch).vm("vm_1").run("sleep 999", { timeout: 2 })),
    (error: unknown) => {
      assert.ok(error instanceof ArkerError);
      assert.equal((error as ArkerError).code, "timeout");
      assert.match((error as ArkerError).message, /continues server-side/);
      return true;
    },
  );
}

async function testPollingGivesUpWhenTheServiceStopsAnswering(): Promise<void> {
  // An unbounded wait is not an infinite one. If the status checks themselves
  // stop answering, waiting forever helps nobody — bail after 10 consecutive.
  const stub = pollingFetch(() => ({ status: 503, body: UNAVAILABLE_ENVELOPE }));
  await assert.rejects(
    withFakeClock(() => pollingClient(stub.fetch).vm("vm_1").run("sleep 999")),
    (error: unknown) => {
      assert.ok(error instanceof ArkerError);
      assert.equal((error as ArkerError).code, "unavailable");
      assert.match((error as ArkerError).message, /10 consecutive poll failures/);
      assert.match((error as ArkerError).message, /last: unavailable/, "the wire code must survive into the summary");
      return true;
    },
  );
  assert.equal(stub.polls, 10, `must stop at exactly 10 failures, polled ${stub.polls}`);
}

async function testAPollBlipDoesNotEndAnUnboundedWait(): Promise<void> {
  // CONSECUTIVE is the whole point: a transient blip must not kill a healthy
  // long-running run, and any answered check resets the counter.
  const stub = pollingFetch((n) => {
    if (n < 3 || (n > 3 && n < 7)) return { status: 503, body: UNAVAILABLE_ENVELOPE };
    return { status: 200, body: n < 20 ? RUNNING_RUN : COMPLETED_RUN };
  });
  const result = await withFakeClock(() => pollingClient(stub.fetch).vm("vm_1").run("long job"));
  assert.equal(result.type, "completed");
  assert.equal((result as CompletedRunResult).exitCode, 0);
}

async function testTimeToBackgroundZeroReturnsTheAckWithoutPolling(): Promise<void> {
  // `time_to_background: 0` is the ONLY spelling of "don't wait" the SDK
  // exposes — `background` was retired, and the two mean the same zero-length
  // sync window anyway, so nothing is lost. ttb=0 answers 202 with a run id
  // without waiting for the command; the client must
  // hand that ack straight back rather than polling a command that is not meant
  // to finish.
  const stub = pollingFetch(() => ({ status: 200, body: RUNNING_RUN }));
  const result = await withFakeClock(() =>
    pollingClient(stub.fetch).vm("vm_1").run("node server.js", { time_to_background: 0 }));
  assert.equal(result.type, "background", "ttb=0 must not wait");
  assert.equal((result as { runId: string }).runId, "run_bg");
  assert.equal(stub.polls, 0, `ttb=0 must not poll; polled ${stub.polls}`);
  assert.equal(stub.posts, 1);
}

await testUnsetTimeoutNeverGivesUpOnAStillRunningRun();
await testExplicitTimeoutStillBoundsTheWait();
await testPollingGivesUpWhenTheServiceStopsAnswering();
await testAPollBlipDoesNotEndAnUnboundedWait();
await testTimeToBackgroundZeroReturnsTheAckWithoutPolling();


// ── Unified sync(): one call, transport chosen internally ────────────────────

function streamCalls(fetch: FakeFetch): FetchCall[] {
  return fetch.calls.filter((call) => call.url.includes("/sync?"));
}

function extractMode(url: string): string | null {
  return new URL(url).searchParams.get("extract");
}

/** Entry names in a (possibly gzipped) tar, read straight from the headers. */
function tarEntryNames(body: Uint8Array, gzipped: boolean): string[] {
  const zlib = require("node:zlib") as typeof import("node:zlib");
  const raw = gzipped ? zlib.gunzipSync(Buffer.from(body)) : Buffer.from(body);
  const names: string[] = [];
  for (let offset = 0; offset + 512 <= raw.length; ) {
    const name = raw.subarray(offset, offset + 100).toString("utf8").replace(/\0.*$/, "");
    if (!name) break;
    const size = parseInt(raw.subarray(offset + 124, offset + 136).toString("utf8").replace(/\0.*$/, "").trim(), 8) || 0;
    names.push(name);
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return names;
}

function tmpTreeBytes(files: Record<string, Uint8Array>): string {
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-sync-"));
  for (const [name, body] of Object.entries(files)) {
    fs.mkdirSync(nodePath.dirname(nodePath.join(dir, name)), { recursive: true });
    fs.writeFileSync(nodePath.join(dir, name), body);
  }
  return dir;
}

async function testSyncFromLocalSmallFileSendsBytes(): Promise<void> {
  // A lone small file has nothing to gain from an archive.
  await withCacheDir(async () => {
    const dir = tmpTree({ "note.txt": "hello world" });
    const fetch = new FakeFetch();
    fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });
    const res = await client(fetch).vm("vm_1").sync("/home/user/note.txt", {
      fromLocal: nodePath.join(dir, "note.txt"),
    });
    assert.equal(res.sent, 1);
    assert.equal(res.bytesSent, 11);
    const call = streamCalls(fetch)[0]!;
    assert.equal(extractMode(call.url), null, "a lone small file needs no archive");
    assert.equal(new URL(call.url).searchParams.get("path"), "/home/user/note.txt");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testSyncFromLocalLargeFileUsesArchive(): Promise<void> {
  await withCacheDir(async () => {
    const dir = tmpTreeBytes({ "blob.bin": new Uint8Array(1024 * 1024 + 1) });
    const fetch = new FakeFetch();
    fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });
    await client(fetch).vm("vm_1").sync("/home/user/blob.bin", {
      fromLocal: nodePath.join(dir, "blob.bin"),
    });
    const mode = extractMode(streamCalls(fetch)[0]!.url);
    assert.ok(mode === "tar" || mode === "tar.gz", `expected an archive, got ${mode}`);
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testSyncFromLocalExecutableUsesArchive(): Promise<void> {
  // Small, but its mode has to survive the trip.
  await withCacheDir(async () => {
    const dir = tmpTree({ tool: "#!/bin/sh\necho hi\n" });
    fs.chmodSync(nodePath.join(dir, "tool"), 0o755);
    const fetch = new FakeFetch();
    fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });
    await client(fetch).vm("vm_1").sync("/usr/local/bin/tool", {
      fromLocal: nodePath.join(dir, "tool"),
    });
    const mode = extractMode(streamCalls(fetch)[0]!.url);
    assert.ok(mode === "tar" || mode === "tar.gz", `expected an archive, got ${mode}`);
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testSyncFromLocalDirSendsOneArchive(): Promise<void> {
  await withCacheDir(async () => {
    const dir = tmpTree({ "a.txt": "alpha", "b.txt": "bravo", "n/c.txt": "charlie" });
    const fetch = syncDirServer();
    const res = await client(fetch).vm("vm_1").sync("/home/user/pkg", { fromLocal: dir });
    assert.equal(res.sent, 3);
    assert.equal(streamCalls(fetch).length, 1, "a whole tree is one request");
    const mode = extractMode(streamCalls(fetch)[0]!.url);
    assert.ok(mode === "tar" || mode === "tar.gz");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testSyncFromLocalDirSkipsUnchanged(): Promise<void> {
  await withCacheDir(async () => {
    const dir = tmpTree({ "a.txt": "same" });
    const hash = createHash("sha256").update("same").digest("hex");
    const fetch = new FakeFetch();
    fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
      ok: true, op: "manifest", entries: [{ path: "a.txt", hash }], truncated: false,
    });
    const res = await client(fetch).vm("vm_1").sync("/home/user/pkg", { fromLocal: dir });
    assert.equal(res.sent, 0);
    assert.equal(res.skipped, 1);
    assert.equal(streamCalls(fetch).length, 0, "nothing changed, nothing sent");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testIncompressiblePayloadIsNotCompressed(): Promise<void> {
  await withCacheDir(async () => {
    const { randomBytes } = await import("node:crypto");
    const files: Record<string, Uint8Array> = {};
    for (let index = 0; index < 4; index++) files[`r${index}.bin`] = randomBytes(256 * 1024);
    const dir = tmpTreeBytes(files);
    const fetch = syncDirServer();
    await client(fetch).vm("vm_1").sync("/home/user/pkg", { fromLocal: dir });
    assert.equal(extractMode(streamCalls(fetch)[0]!.url), "tar",
      "random bytes do not compress, so compression must be skipped");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testCompressiblePayloadIsCompressed(): Promise<void> {
  await withCacheDir(async () => {
    const body = "def hello(): return 'world'\n".repeat(12000);
    const dir = tmpTree({ "t0.py": body, "t1.py": body, "t2.py": body, "t3.py": body });
    const fetch = syncDirServer();
    await client(fetch).vm("vm_1").sync("/home/user/pkg", { fromLocal: dir });
    assert.equal(extractMode(streamCalls(fetch)[0]!.url), "tar.gz");
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testSyncToLocalFile(): Promise<void> {
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-dl-"));
  const fetch = new FakeFetch();
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
    ok: true, op: "manifest", entries: [], truncated: false,
  });
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
    ok: true, op: "read", path: "/home/user/out.txt", size: 5, content: "hello", encoding: "utf8",
  });
  const res = await client(fetch).vm("vm_1").sync("/home/user/out.txt", {
    toLocal: nodePath.join(dir, "out.txt"),
  });
  assert.equal(fs.readFileSync(nodePath.join(dir, "out.txt"), "utf8"), "hello");
  assert.equal(res.sent, 1);
  fs.rmSync(dir, { recursive: true, force: true });
}

async function testSyncToLocalDirReadsEachFile(): Promise<void> {
  // No bulk read exists, so a directory download is one request per file.
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-dl-"));
  const fetch = new FakeFetch();
  fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
    ok: true, op: "manifest", truncated: false,
    entries: [{ path: "a.txt", hash: "0".repeat(64) }, { path: "nested/b.txt", hash: "1".repeat(64) }],
  });
  for (const body of ["A", "B"]) {
    fetch.addJson((m, url) => m === "POST" && url.endsWith("/sync"), 200, {
      ok: true, op: "read", path: "x", size: 1, content: body, encoding: "utf8",
    });
  }
  const res = await client(fetch).vm("vm_1").sync("/home/user/pkg", { toLocal: dir });
  assert.equal(res.sent, 2);
  assert.ok(fs.existsSync(nodePath.join(dir, "a.txt")));
  assert.ok(fs.existsSync(nodePath.join(dir, "nested", "b.txt")));
  fs.rmSync(dir, { recursive: true, force: true });
}

async function testSyncFromLocalRenamesOnUpload(): Promise<void> {
  // The destination basename differs from the local one, and the file is big
  // enough to travel as an archive — the archive entry must still carry the
  // DESTINATION name, or the file lands under the wrong path.
  await withCacheDir(async () => {
    const dir = tmpTreeBytes({ "orig.bin": new Uint8Array(1024 * 1024 + 1) });
    const fetch = new FakeFetch();
    fetch.addJson((m, url) => m === "POST" && url.includes("/sync?"), 200, { ok: true });
    const res = await client(fetch).vm("vm_1").sync("/home/user/renamed.bin", {
      fromLocal: nodePath.join(dir, "orig.bin"),
    });
    assert.equal(res.sent, 1);
    const call = streamCalls(fetch)[0]!;
    assert.equal(new URL(call.url).searchParams.get("path"), "/home/user",
      "an archive lands in the destination DIRECTORY");
    const mode = extractMode(call.url);
    assert.ok(mode === "tar" || mode === "tar.gz");
    // The archive must name the entry after the DESTINATION, not the source.
    assert.ok(call.bodyBytes && call.bodyBytes.length > 0, "the archive body was captured");
    const names = tarEntryNames(call.bodyBytes!, mode === "tar.gz");
    assert.deepEqual(names, ["renamed.bin"], `archive entries were ${JSON.stringify(names)}`);
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

async function testSyncDirAliasStillWorks(): Promise<void> {
  await withCacheDir(async () => {
    const dir = tmpTree({ "a.txt": "alpha" });
    const res = await client(syncDirServer()).vm("vm_1").syncDir(dir, "/p");
    assert.equal(res.sent, 1);
    fs.rmSync(dir, { recursive: true, force: true });
  });
}

await testSyncFromLocalSmallFileSendsBytes();
await testSyncFromLocalLargeFileUsesArchive();
await testSyncFromLocalExecutableUsesArchive();
await testSyncFromLocalDirSendsOneArchive();
await testSyncFromLocalDirSkipsUnchanged();
await testIncompressiblePayloadIsNotCompressed();
await testCompressiblePayloadIsCompressed();
await testSyncToLocalFile();
await testSyncToLocalDirReadsEachFile();
await testSyncFromLocalRenamesOnUpload();
await testSyncDirAliasStillWorks();
console.log("PASS unified sync");
