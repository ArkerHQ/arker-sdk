import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import nodePath from "node:path";
import { createHash } from "node:crypto";

import {
  Arker,
  ArkerError,
  VM,
  discoverRegions,
  type CompletedRunResult,
  type PtyWebSocketFactory,
} from "../src/index.js";
import { runPollBudgetMs } from "../src/internal/run-poll.js";
import { parseDockerfile, DockerfileError } from "../src/buildSpec.js";
import { applySteps, BuildError, type BuildTarget } from "../src/build.js";
import { isExpectedSurfaceStub } from "./helpers/surface-errors.js";

type FetchCall = {
  url: string;
  method: string;
  body?: string;
  headers: Record<string, string>;
};

type FetchScript = {
  predicate: (method: string, url: string) => boolean;
  response: Response | Error;
};

class FakeFetch {
  readonly calls: FetchCall[] = [];
  private readonly script: FetchScript[] = [];

  addJson(predicate: FetchScript["predicate"], status: number, body: unknown): void {
    this.script.push({
      predicate,
      response: new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    });
  }

  addNetworkError(predicate: FetchScript["predicate"], message = "response lost"): void {
    this.script.push({ predicate, response: new TypeError(message) });
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? init.body : undefined;
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries((init?.headers ?? {}) as Record<string, string>)) {
      headers[key.toLowerCase()] = value;
    }
    this.calls.push({ url, method, body, headers });

    const index = this.script.findIndex((entry) => entry.predicate(method, url));
    assert.notEqual(index, -1, `no scripted response for ${method} ${url}`);
    const response = this.script.splice(index, 1)[0]!.response;
    if (response instanceof Error) throw response;
    return response;
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

const UNKNOWN_OUTCOME_ERROR_BODY = {
  error: {
    code: "unavailable",
    message: "operation outcome is unknown; reconcile resource state before retry",
    timestamp: "2026-01-01T00:00:00.000Z",
    retryable: false,
  },
};

function decode(bytes: Uint8Array): string {
  return new TextDecoder().decode(bytes);
}

async function testForkPostsDirectlyToSourceVm(): Promise<void> {
  const fetch = new FakeFetch();
  // VM.fork() uses the top-level operation and supplies its own ID.
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://attached.invalid/api/v1/fork",
    200,
    {
      vm_id: "vm_child",
      owner_org_id: "owner",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
      tunnels: [],
    },
  );

  const source = new VM(client(fetch), "source-vm-id", "https://attached.invalid/api");
  const vm = await source.fork({
    source_vm_id: "ignored",
    name: "demo",
    description: "CI runner",
    ssh_public_keys: ["ssh-ed25519 AAAA test@example.com"],
  });

  assert.equal(vm.id, "vm_child");
  assert.equal(vm.baseUrl, "https://attached.invalid/api");
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

async function testForkPreservesTheCanonicalWireShape(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url.endsWith("/v1/fork"),
    200,
    {
      vm_id: "vm_child",
      owner_org_id: "o",
      created_at: "now",
      public: false,
      state: "idle",
      sessions: [],
    },
  );

  await client(fetch).fork({
    source_vm_name: "ubuntu",
    source_org_name: "ArkerHQ",
    resources: { vcpu: 2, memory_mib: 2048 },
    description: null,
    disk: false,
    layers: ["disk"],
  });

  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), {
    source_vm_name: "ubuntu",
    source_org_name: "ArkerHQ",
    resources: { vcpu: 2, memory_mib: 2048 },
    description: null,
    disk: false,
    layers: ["disk"],
  });
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
  assert.equal(body.source_vm_id, undefined, "no VM selector may accompany an image");
  assert.equal(body.source_vm_name, undefined, "no VM selector may accompany an image");
}

async function testForkFromDockerfileBuildsFromItsBaseImage(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    { vm_id: "vm_build", owner_org_id: "o", created_at: "now", public: false, state: "idle", sessions: [] },
  );
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_build/runs",
    200,
    {
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: 0,
    },
  );

  await client(fetch).fork({ dockerfile: "FROM ubuntu:24.04\nRUN echo hi\n" });

  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { image: "ubuntu:24.04" });
  assert.equal(JSON.parse(fetch.calls[1]!.body!).command, "echo hi");
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

  const vm = await client(fetch).fork({
    source_vm_name: "catalog-template",
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
      tunnels: [],
    },
  );

  await client(fetch).fork({ source_vm_name: "catalog-template" });

  const body = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(body.policies, undefined);
  assert.equal(body.ssh_public_keys, undefined);
}

async function testRemovedRunNetworkInputsFailBeforeRequests(): Promise<void> {
  const fetch = new FakeFetch();
  const isBadRequest = (error: unknown) =>
    error instanceof ArkerError && error.code === "bad_request";
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

async function testUpdateSendsPolicies(): Promise<void> {
  const vm = {
    vm_id: "vm_1",
    owner_org_id: "owner",
    created_at: "now",
    public: false,
    state: "idle",
    sessions: [],
    resources: {},
    network: {},
  };
  const isPatch = (method: string, url: string) => method === "PATCH" && url.endsWith("/v1/vms/vm_1");
  const doc = {
    policies: [
      {
        type: "outbound" as const,
        match: { hosts: ["example.com"] },
        action: "allow" as const,
      },
    ],
  };

  // A policy document rides on PATCH /v1/vms/{id} as a top-level `policies`
  // field, alongside the other patchable fields.
  const fetch = new FakeFetch();
  fetch.addJson(isPatch, 200, vm);
  await client(fetch).vm("vm_1").update({ description: "locked down", policies: doc });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { description: "locked down", policies: doc });

  // The flat resource form folds vcpu/memory/disk into `resources` and still
  // carries `policies` through untouched.
  fetch.addJson(isPatch, 200, vm);
  await client(fetch).vm("vm_1").update({ vcpu: 2, policies: doc });
  assert.deepEqual(JSON.parse(fetch.calls[1]!.body!), {
    resources: { vcpu: 2, memory_mib: null, disk_mib: null },
    policies: doc,
  });

  // An empty document is a real replacement (clears to allow-all), so it must
  // not be pruned like an omitted field.
  fetch.addJson(isPatch, 200, vm);
  await client(fetch).vm("vm_1").update({ policies: { policies: [] } });
  assert.deepEqual(JSON.parse(fetch.calls[2]!.body!), { policies: { policies: [] } });

  // Omitting `policies` leaves the current policy alone: no key on the wire.
  fetch.addJson(isPatch, 200, vm);
  await client(fetch).vm("vm_1").update({ vcpu: 4 });
  assert.equal(JSON.parse(fetch.calls[3]!.body!).policies, undefined);

  // `vgpu` is a resource like any other: alone in the flat form it must still
  // build `resources`, or a GPU resize is silently dropped.
  fetch.addJson(isPatch, 200, vm);
  await client(fetch).vm("vm_1").update({ vgpu: 0.25 });
  assert.deepEqual(JSON.parse(fetch.calls[4]!.body!), {
    resources: { vcpu: null, memory_mib: null, disk_mib: null, vgpu: 0.25 },
  });

  // A CPU-only resize must not carry `vgpu` at all — it is mutually exclusive
  // with the hardware GPU fields, so a null is not a safe stand-in.
  fetch.addJson(isPatch, 200, vm);
  await client(fetch).vm("vm_1").update({ memory_mib: 2048 });
  assert.deepEqual(JSON.parse(fetch.calls[5]!.body!), {
    resources: { vcpu: null, memory_mib: 2048, disk_mib: null },
  });
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
      tunnels: [],
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

async function testWhoamiUsesControlPlane(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://control.invalid/api/v1/whoami",
    200,
    { org_id: "org_01", org_name: "ArkerHQ" },
  );
  const arker = new Arker({
    apiKey: "ark_test",
    controlBaseUrl: "https://control.invalid/api",
    fetch: fetch.fetch,
    retry: false,
  });

  assert.deepEqual(await arker.whoami(), { org_id: "org_01", org_name: "ArkerHQ" });
  assert.throws(() => arker.vm("vm_01"), /provider and region or baseUrl are required/i);
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
    (method, url) => method === "GET" && url === "https://control.invalid/api/v1/runs?since=10&until=20&vm=vm_1&vms=vm_2%2Cvm_3&region=us-west-2&provider=aws&search=pytest&limit=25&offset=5&lite=true&runtime=fc&endpoint=run&actions=run%2Cfork&status=success%2Cinternal&status_min=200&status_max=599&sort=when&dir=asc",
    200,
    {
      since: 10,
      until: 20,
      limit: 25,
      offset: 5,
      lite: true,
      rows: [{
        source: "arkerd",
        t_ms: 10,
        request_id: "req_1",
        run_id: "run_1",
        vm_id: "vm_1",
        session_id: "session_1",
        region: "us-west-2",
        status: 200,
        total_ms: 12.5,
        queue_ms: 1.5,
        lambda_call_ms: 0,
        lambda_duration_ms: 0,
        executor_duration_ms: 10,
        executor_kind: "firecracker",
        executor_cpu_ms: 8,
        executor_mem_mb: 64,
        lambda_cpu_ms: 0,
        lambda_mem_mb: 0,
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
    runtime: "fc",
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
        tunnels: [],
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
      tunnels: [],
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
      tunnels: [],
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

async function testGetRetriesNetworkFailure(): Promise<void> {
  const fetch = new FakeFetch();
  const match = (method: string, url: string) => method === "GET" && url.endsWith("/v1/vms/vm_1");
  fetch.addNetworkError(match);
  fetch.addJson(match, 200, {
    vm_id: "vm_1", owner_org_id: "owner", created_at: "now",
    public: false, state: "idle", sessions: [], network: {}, resources: {},
  });

  const vm = await clientWithRetry(fetch, 2).getVm("vm_1");

  assert.equal(vm.id, "vm_1");
  assert.equal(fetch.calls.length, 2);
}

async function testKeyedRunDoesNotRetryAmbiguousNetworkFailure(): Promise<void> {
  const fetch = new FakeFetch();
  const match = (method: string, url: string) => method === "POST" && url.endsWith("/v1/vms/vm_1/runs");
  fetch.addNetworkError(match);

  await assert.rejects(
    () => clientWithRetry(fetch, 2).vm("vm_1").run(
      "touch /tmp/once",
      { time_to_background: 0, idempotencyKey: "run-key" },
    ),
    (error: unknown) => error instanceof ArkerError
      && error.code === "unavailable"
      && error.message.includes("outcome is unknown")
      && error.message.toLowerCase().includes("reconcile"),
  );
  assert.equal(fetch.calls.length, 1);
  assert.equal(fetch.calls[0]?.headers["idempotency-key"], "run-key");
}

async function testMutationDoesNotRetryServerUnknownOutcome(): Promise<void> {
  const fetch = new FakeFetch();
  const match = (method: string, url: string) => method === "POST" && url.endsWith("/v1/vms/vm_1/runs");
  fetch.addJson(match, 503, UNKNOWN_OUTCOME_ERROR_BODY);
  fetch.addJson(match, 200, { run_id: "run_2", exit_code: 0 });

  await assert.rejects(
    () => clientWithRetry(fetch, 2).vm("vm_1").run("touch /tmp/once", { time_to_background: 0 }),
    (error: unknown) => error instanceof ArkerError
      && error.code === "unavailable"
      && error.status === 503
      && error.message.includes("outcome is unknown"),
  );
  assert.equal(fetch.calls.length, 1);
}

async function testSyncStreamDoesNotRetryAmbiguousNetworkFailure(): Promise<void> {
  const fetch = new FakeFetch();
  const match = (method: string, url: string) => method === "POST" && url.includes("/sync-stream");
  fetch.addNetworkError(match);

  await assert.rejects(
    () => clientWithRetry(fetch, 2).vm("vm_1").sync("/home/user/x", "content"),
    (error: unknown) => error instanceof ArkerError && error.message.includes("outcome is unknown"),
  );
  assert.equal(fetch.calls.length, 1);
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
await testForkPreservesTheCanonicalWireShape();
await testForkFromImageSendsOnlyTheImage();
await testForkFromDockerfileBuildsFromItsBaseImage();
await testForkOmitsSourceOrgWhenNotExplicit();
await testForkOmitsUnconfiguredCapabilities();
await testRemovedRunNetworkInputsFailBeforeRequests();
await testUpdateSendsTopLevelSshPublicKeys();
await testUpdateSendsPolicies();
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
await testWhoamiUsesControlPlane();
await testDiscoverRegionsRequiresNoConfiguredClient();
await testListedVmUsesItsPlacementEndpoint();
testExplicitVmHandleUsesPlacementEndpoint();
await testListRunsUsesControlPlaneAndFilters();
await testListVmsPreservesForkLimitFields();
await testForkSendsDurableFlag();
await testRunSendsIdempotencyKeyHeader();
await testRunStatusReturnsRetryCount();
async function testConnectPtyPassesCancelTtlSecs(): Promise<void> {
  // ARK-120: cancelTtlSecs must surface as the `cancel_ttl_secs` query param so
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
await testGetRetriesNetworkFailure();
await testKeyedRunDoesNotRetryAmbiguousNetworkFailure();
await testMutationDoesNotRetryServerUnknownOutcome();
await testSyncStreamDoesNotRetryAmbiguousNetworkFailure();
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
  const vm = await client.fork({ source_vm_name: "source-vm" });
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
  // fork() passes the caller's retry window to the shared transport.
  const fetchImpl = new FakeFetch();
  fetchImpl.addJson((m, u) => m === "POST" && u.includes("/fork"), 200, { vm_id: "vm-9", state: "running" });
  const client = new Arker({ apiKey: "k", baseUrl: "http://x", fetch: fetchImpl.fetch, retry: false });
  await client.fork({ source_vm_name: "arkuntu", queueing_timeout: 30 });
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

// ── syncDir tarball compression ──────────────────────────────────────
// syncDir packs changed files into ONE tarball and uploads it in a single
// request whose cost the host pays again on commit. Uncompressed, a large tree
// blew the sync budget and failed outright (a Linux checkout is 319 MB raw,
// 70 MB gzipped). gzip is therefore load-bearing, not an optimization.

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
  // 2. no /sync-stream on this server -> 404 -> legacy upload+run path below
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync-stream"), 404, {
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

// ── syncDir /sync-stream fast path ───────────────────────────────────
// The legacy path is upload + a SEPARATE run("tar -xf"): two round-trips, with
// the extract going through the USER run scheduler where it queues behind an
// active foreground run. /sync-stream?extract=tar.gz does both in one request,
// untarring in the guest before responding.

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
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync-stream"), 200, { ok: true });

  await client(fetch).vm("vm_1").syncDir(dir, "/home/user/p");

  const stream = fetch.calls.find((c) => c.url.includes("/sync-stream"));
  assert.ok(stream, "syncDir must try /sync-stream first");
  assert.equal(stream!.headers["content-type"], "application/octet-stream", "body must go raw, not base64 JSON");

  // Params ride in the query string: the auth middleware strips x-arker-* from
  // untrusted callers and would erase them as headers.
  const qs = new URL(stream!.url).searchParams;
  assert.equal(qs.get("path"), "/home/user/p");
  assert.equal(qs.get("extract"), "tar.gz");
  assert.ok(Number(qs.get("size")) > 0, "size must be the tarball byte length");

  // The whole point: no second round-trip through the user run scheduler.
  assert.equal(fetch.calls.filter((c) => c.url.endsWith("/runs")).length, 0, "fast path must not issue an extract run");
  cleanup();
}

async function testSyncStreamErrorsOtherThan404DoNotFallBack(): Promise<void> {
  const { dir, fetch, cleanup } = await syncDirFixture();
  // A path escape is a REAL rejection. Silently retrying the slow path would
  // turn a hard error into a confusing one.
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync-stream"), 403, {
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

// ── syncDir assumeEmpty ──────────────────────────────────────────────
// The manifest exists to avoid re-sending unchanged files. Into a fresh
// directory it is guaranteed empty, so the round-trip costs ~184ms to learn
// nothing — on exactly the first-sync path we lose to E2B on.

async function testAssumeEmptySkipsTheManifestRoundTrip(): Promise<void> {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const nodePath = await import("node:path");
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "syncdir-assume-"));
  fs.writeFileSync(nodePath.join(dir, "a.ts"), "export const a = 1;\n");

  const fetch = new FakeFetch();
  // Deliberately NO manifest script: if syncDir asks for one, FakeFetch throws.
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync-stream"), 200, { ok: true });

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

// ── fork Idempotency-Key ────────────────────────────────────────────────
//
// The prod failure: the gateway answered a fork with 502/504 AFTER the worker
// had already built the VM, the retry re-POSTed, and the caller got a SECOND
// machine while the first ran on, unnamed and billable. Transport failures on
// a mutation are not retried, but a 502/504 is a *response* and still
// is -- so this is the window that remains.

const FORK_VM = {
  vm_id: "vm_child",
  owner_org_id: "owner",
  created_at: "now",
  description: null,
  public: false,
  state: "idle",
  sessions: [],
  network: {},
  resources: {},
};

function forkFetch(...statuses: number[]): FakeFetch {
  const fetch = new FakeFetch();
  for (const status of statuses) {
    fetch.addJson(
      (method, url) => method === "POST" && url.endsWith("/v1/fork"),
      status,
      status < 400 ? FORK_VM : { error: { code: "bad_gateway", message: "lost" } },
    );
  }
  return fetch;
}

async function testForkSendsNoKeyUnlessAsked(): Promise<void> {
  // OFF is the default. An unkeyed fork is never deduplicated, which is the
  // API's own behaviour -- a caller who says nothing must get exactly that.
  const fetch = forkFetch(200);

  await client(fetch).fork({ source_vm_id: "source-vm-id" });

  assert.equal(fetch.calls[0]!.headers["idempotency-key"], undefined);
}

async function testForkGeneratesAKeyWhenIdempotencyIsRequested(): Promise<void> {
  const fetch = forkFetch(200);

  await client(fetch).fork({ source_vm_id: "source-vm-id", idempotency: true });

  const call = fetch.calls[0]!;
  assert.match(call.headers["idempotency-key"] ?? "", /^sdk-fork-/);
  // Headers, not contract fields: the server's validator rejects unknown body
  // keys, so a leak of either would 400 every fork.
  const body = JSON.parse(call.body!);
  assert.equal(body.idempotencyKey, undefined);
  assert.equal(body.idempotency, undefined);
}

async function testAnExplicitKeyWinsOverTheFlag(): Promise<void> {
  const fetch = forkFetch(200);

  await client(fetch).fork({ source_vm_id: "source-vm-id", idempotency: true, idempotencyKey: "caller-chosen" });

  assert.equal(fetch.calls[0]!.headers["idempotency-key"], "caller-chosen");
}

async function testForkRetryReusesTheSameIdempotencyKey(): Promise<void> {
  // The load-bearing one: the retried attempt must present the FIRST key. A
  // fresh key per attempt looks identical in every other test and still
  // builds the duplicate.
  const fetch = forkFetch(502, 200);

  await clientWithRetry(fetch, 2).fork({ source_vm_id: "source-vm-id", idempotency: true });

  assert.equal(fetch.calls.length, 2, "expected a retry");
  const first = fetch.calls[0]!.headers["idempotency-key"];
  // Non-empty as well as equal: two blank keys are also "the same key".
  assert.ok(first, "the fork sent an empty Idempotency-Key");
  assert.equal(first, fetch.calls[1]!.headers["idempotency-key"]);
}

async function testForkUsesAnExplicitIdempotencyKeyVerbatim(): Promise<void> {
  const fetch = forkFetch(200);

  await client(fetch).fork({ source_vm_id: "source-vm-id", idempotencyKey: "caller-chosen" });

  assert.equal(fetch.calls[0]!.headers["idempotency-key"], "caller-chosen");
}

async function testVmForkAlsoCarriesAKey(): Promise<void> {
  // `vm.fork()` is a second entry point; it delegates to _fork, and this is
  // what proves the option survives that hop rather than being dropped.
  const fetch = forkFetch(200);

  await client(fetch).vm("vm_1").fork({ idempotencyKey: "from-a-handle" });

  const call = fetch.calls[0]!;
  assert.equal(call.headers["idempotency-key"], "from-a-handle");
  assert.equal(JSON.parse(call.body!).idempotencyKey, undefined);
}

async function testTwoForksDoNotShareAGeneratedKey(): Promise<void> {
  const fetch = forkFetch(200, 200);
  const arker = client(fetch);

  await arker.fork({ source_vm_id: "source-vm-id", idempotency: true });
  await arker.fork({ source_vm_id: "source-vm-id", idempotency: true });

  const [a, b] = [fetch.calls[0]!.headers["idempotency-key"], fetch.calls[1]!.headers["idempotency-key"]];
  // Non-empty as well as distinct: two absent keys are also "not equal", which
  // would let a generator that emits nothing pass this.
  assert.ok(a && b, "a requested fork sent no key");
  assert.notEqual(a, b);
}

async function testGeneratedForkKeyFitsTheServerLimit(): Promise<void> {
  // The handler rejects anything over 64 characters before it forks.
  const fetch = forkFetch(200);

  await client(fetch).fork({ source_vm_id: "source-vm-id", idempotency: true });

  const key = fetch.calls[0]!.headers["idempotency-key"]!;
  assert.ok(key.length > 0 && key.length <= 64, `generated key is ${key.length} chars`);
}

await testForkSendsNoKeyUnlessAsked,
  testForkGeneratesAKeyWhenIdempotencyIsRequested,
  testAnExplicitKeyWinsOverTheFlag();
await testForkRetryReusesTheSameIdempotencyKey();
await testForkUsesAnExplicitIdempotencyKeyVerbatim();
await testVmForkAlsoCarriesAKey();
await testTwoForksDoNotShareAGeneratedKey();
await testGeneratedForkKeyFitsTheServerLimit();

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
  fetch.addJson((m, url) => m === "POST" && url.includes("/sync-stream"), 200, { ok: true });
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

    fs.writeFileSync(abs, "BBBBB"); // same length, different content
    const now = fs.statSync(abs, { bigint: true });

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
    fetch.addJson((m, url) => m === "POST" && url.includes("/sync-stream"), 500, {
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
  // exposes — #89 removed `background`, and arkerd resolves the two to the same
  // zero-length sync window anyway, so nothing is lost. Measured against the
  // live service, ttb=0 answers 202 with a run id in ~0.2s; the client must
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

// ── Dockerfile builds ────────────────────────────────────────────────────
//
// The build runs from the CLIENT, not the server, and COPY is why: its sources
// are files on this machine, which no server-side build could reach without
// being handed a build context first. These cover the part that is ours:
// parsing, ordering, shell state, and how COPY resolves against the context.

class FakeBuildVM implements BuildTarget {
  readonly calls: { kind: string; a: string; b?: string }[] = [];
  readonly ignores: ((rel: string) => boolean)[] = [];
  readonly runOptions: (Record<string, unknown> | undefined)[] = [];
  constructor(private readonly exitCodes: Record<string, number> = {}) {}
  async run(command: string, options?: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ kind: "run", a: command });
    this.runOptions.push(options);
    let code = 0;
    for (const [needle, value] of Object.entries(this.exitCodes)) {
      if (command.includes(needle)) code = value;
    }
    return { exit_code: code, stdout: "", stderr: "" };
  }
  async sync(path: string, data: Uint8Array | string): Promise<void> {
    this.calls.push({ kind: "sync", a: path, b: String(data.length) });
  }
  async syncDir(localDir: string, remoteDir: string, options?: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ kind: "syncDir", a: nodePath.basename(localDir), b: remoteDir });
    this.ignores.push((options?.ignore as ((rel: string) => boolean)) ?? (() => false));
    return undefined;
  }
  get commands(): string[] {
    return this.calls.filter((c) => c.kind === "run").map((c) => c.a);
  }
}

function makeBuildContext(): string {
  const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), "arker-build-"));
  fs.writeFileSync(nodePath.join(dir, "app.js"), "console.log(1)\n");
  fs.writeFileSync(nodePath.join(dir, "package.json"), "{}\n");
  fs.writeFileSync(nodePath.join(dir, "package-lock.json"), "{}\n");
  fs.writeFileSync(nodePath.join(dir, "ignored.txt"), "no\n");
  fs.mkdirSync(nodePath.join(dir, "src"));
  fs.writeFileSync(nodePath.join(dir, "src", "index.js"), "//\n");
  return dir;
}

async function buildAgainst(text: string, contextRoot: string): Promise<FakeBuildVM> {
  const vm = new FakeBuildVM();
  await applySteps(vm, parseDockerfile(text).steps, contextRoot);
  return vm;
}

function testDockerfileParsingBasics(): void {
  const parsed = parseDockerfile(
    "# comment\nFROM ubuntu:24.04\nRUN apt-get update \\\n  && echo done\nENV A=1 B=2\n",
  );
  assert.equal(parsed.baseImage, "ubuntu:24.04");
  assert.equal(parsed.steps.length, 2, JSON.stringify(parsed.steps));
  // Comment dropped and the continuation joined, both by the parser library.
  assert.equal(parsed.steps[0]!.kind, "run");
  assert.ok((parsed.steps[0] as { command: string }).command.includes("&& echo done"));
  assert.deepEqual(
    (parsed.steps[1] as { pairs: [string, string][] }).pairs,
    [["A", "1"], ["B", "2"]],
  );
}

// Docker's SHELL replaces the interpreter for the SHELL FORM of RUN. Its usual
// real-world use is ["/bin/bash", "-o", "pipefail", "-c"]: plain sh reports only
// the last exit code in a pipeline, so a failed download in `curl bad | tar xz`
// looks green. Kept in step with the Python SDK, which parses the same files.
function testDockerfileShell(): void {
  const applied = parseDockerfile(
    'FROM ubuntu:24.04\nSHELL ["/bin/bash", "-o", "pipefail", "-c"]\nRUN curl -f url | tar xz\n',
  );
  assert.deepEqual(
    applied.steps.map((step) => (step as { command: string }).command),
    ["/bin/bash -o pipefail -c 'curl -f url | tar xz'"],
  );

  // Applies forward only, and the last SHELL wins.
  const ordered = parseDockerfile(
    'FROM a\nRUN echo early\nSHELL ["/bin/bash", "-c"]\nSHELL ["/bin/zsh", "-c"]\nRUN echo late\n',
  );
  assert.deepEqual(
    ordered.steps.map((step) => (step as { command: string }).command),
    ["echo early", "/bin/zsh -c 'echo late'"],
  );

  // Exec form does not go through an interpreter, so SHELL cannot apply.
  const execForm = parseDockerfile('FROM a\nSHELL ["/bin/bash", "-c"]\nRUN ["echo", "hi"]\n');
  assert.deepEqual(
    execForm.steps.map((step) => (step as { command: string }).command),
    ["echo hi"],
  );

  // Docker rewrites the shell form of CMD and ENTRYPOINT too, not just RUN.
  const entry = parseDockerfile(
    'FROM a\nSHELL ["/bin/bash", "-c"]\nENTRYPOINT echo hi\nCMD echo bye\n',
  );
  assert.deepEqual(
    entry.steps.map((step) => (step as { value: string }).value),
    ["/bin/bash -c 'echo hi'", "/bin/bash -c 'echo bye'"],
  );

  // ...and leaves their exec form alone, as for RUN.
  const entryExec = parseDockerfile(
    'FROM a\nSHELL ["/bin/bash", "-c"]\nENTRYPOINT ["echo", "hi"]\nCMD ["echo", "bye"]\n',
  );
  assert.deepEqual(
    entryExec.steps.map((step) => (step as { value: string }).value),
    ["echo hi", "echo bye"],
  );

  // Instructions that become driver-generated export/cd never take it.
  const other = parseDockerfile('FROM a\nSHELL ["/bin/bash", "-c"]\nENV A=b\nWORKDIR /app\n');
  assert.deepEqual(other.steps, [
    { kind: "env", pairs: [["A", "b"]] },
    { kind: "workdir", path: "/app" },
  ]);

  // Byte-identical to the Python SDK, which asserts this same string. The two
  // parsers read the same Dockerfiles, so their quoting must not diverge.
  const quoted = parseDockerfile('FROM a\nSHELL ["/bin/bash", "-c"]\nRUN echo \'hi\'\n');
  assert.deepEqual(
    quoted.steps.map((step) => (step as { command: string }).command),
    [`/bin/bash -c 'echo '"'"'hi'"'"''`],
  );

  for (const [text, needle] of [
    ['FROM a\nSHELL /bin/bash -c\n', "exec form"],
    ['FROM a\nSHELL []\n', "at least one"],
  ] as [string, string][]) {
    assert.throws(
      () => parseDockerfile(text),
      (error: unknown) => error instanceof DockerfileError && error.message.includes(needle),
      `expected ${needle} named for: ${text}`,
    );
  }
}

function testDockerfileRefusalsAreNamed(): void {
  const cases: [string, string][] = [
    ["RUN echo hi\n", "FROM"],
    ["FROM a AS x\nFROM b\n", "multi-stage"],
    ["FROM a\nCOPY --from=x /a /a\n", "--from"],
    ["ARG B=a\nFROM ${B}\n", "ARG"],
    ["FROM a\nVOLUME /data\n", "VOLUME"],
    ["FROM a\nADD ./local /d\n", "ADD"],
  ];
  for (const [text, needle] of cases) {
    assert.throws(
      () => parseDockerfile(text),
      (error: unknown) => error instanceof DockerfileError && error.message.includes(needle),
      `expected ${needle} named for: ${text}`,
    );
  }
}

async function testBuildAppliesShellStateInOrder(): Promise<void> {
  const dir = makeBuildContext();
  try {
    const vm = await buildAgainst('FROM x\nWORKDIR /app\nENV G="hello world"\nRUN pwd\n', dir);
    // WORKDIR is created before it is entered: Docker makes a missing one, and
    // a bare `cd` would fail on a fresh image.
    assert.equal(vm.commands[0], "mkdir -p /app && cd /app");
    // Quoted only where needed, so a value with a space survives intact.
    assert.equal(vm.commands[1], "export G='hello world'");
    assert.equal(vm.commands[2], "pwd");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testUserWrapsOnlyLaterRuns(): Promise<void> {
  const dir = makeBuildContext();
  try {
    const vm = await buildAgainst("FROM x\nRUN first\nUSER app\nRUN second\n", dir);
    assert.equal(vm.commands[0], "first");
    assert.equal(vm.commands[1], "su -p app -c second");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testCopyResolvesAgainstTheContext(): Promise<void> {
  const dir = makeBuildContext();
  try {
    let vm = await buildAgainst("FROM x\nCOPY app.js /srv/app.js\n", dir);
    assert.ok(
      vm.calls.some((c) => c.kind === "sync" && c.a === "/srv/app.js"),
      JSON.stringify(vm.calls),
    );

    // A directory source copies its CONTENTS to the destination.
    vm = await buildAgainst("FROM x\nCOPY src /app/src\n", dir);
    assert.ok(
      vm.calls.some((c) => c.kind === "syncDir" && c.a === "src" && c.b === "/app/src"),
      JSON.stringify(vm.calls),
    );

    // A glob matches what it should and nothing else.
    vm = await buildAgainst("FROM x\nCOPY package*.json /app/\n", dir);
    const synced = vm.calls.filter((c) => c.kind === "sync").map((c) => c.a).sort();
    assert.deepEqual(synced, ["/app/package-lock.json", "/app/package.json"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testCopyRefusesToEscapeTheContext(): Promise<void> {
  const dir = makeBuildContext();
  try {
    // The context root is the boundary, exactly as `docker build` treats it.
    await assert.rejects(
      () => buildAgainst("FROM x\nCOPY ../secrets /app\n", dir),
      (error: unknown) =>
        error instanceof BuildError && error.message.includes("outside the build context"),
    );
    await assert.rejects(
      () => buildAgainst("FROM x\nCOPY nope.txt /app\n", dir),
      (error: unknown) => error instanceof BuildError && error.message.includes("no such file"),
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testCopyIsNotWrappedInTheUserShell(): Promise<void> {
  const dir = makeBuildContext();
  try {
    // Docker writes copied files as root regardless of USER unless --chown
    // says otherwise, and the sync APIs write as the guest agent.
    const vm = await buildAgainst("FROM x\nUSER app\nCOPY --chown=app:app src /app/src\n", dir);
    assert.ok(vm.calls.some((c) => c.kind === "syncDir"));
    assert.equal(vm.commands.at(-1), "chown -R app:app /app/src");
    assert.ok(!vm.commands.some((c) => c.startsWith("su -p")), vm.commands.join(" | "));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testAddUrlIsFetchedByTheClient(): Promise<void> {
  // Docker downloads an ADD url on the BUILDER and copies the bytes in, so the
  // image needs no curl or wget. Fetching inside the guest instead made ADD
  // fail on any minimal base (ubuntu:24.04 ships neither) — a divergence from
  // Docker, not a limitation of it.
  const dir = makeBuildContext();
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 })) as typeof fetch;
  try {
    const vm = new FakeBuildVM();
    // A public IP LITERAL, so the SSRF guard's DNS lookup is skipped and the
    // test needs no network. A hostname would be resolved for real.
    await applySteps(vm, parseDockerfile("FROM x\nADD https://93.184.216.34/f /opt/f\n").steps, dir);
    assert.ok(
      vm.calls.some((c) => c.kind === "sync" && c.a === "/opt/f" && c.b === "4"),
      JSON.stringify(vm.calls),
    );
    // Nothing is asked of the guest: no curl, no wget, no shell at all.
    assert.deepEqual(vm.commands, []);
  } finally {
    globalThis.fetch = realFetch;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testAFailingRunAbortsTheBuild(): Promise<void> {
  // Docker fails a build on a non-zero RUN, and so must this. Without it a
  // Dockerfile whose `RUN npm install` fails hands back a VM that looks built
  // and is not, with every later instruction applied on top of the failure.
  const dir = makeBuildContext();
  try {
    const vm = new FakeBuildVM({ boom: 17 });
    await assert.rejects(
      () => applySteps(vm, parseDockerfile("FROM x\nRUN ok-one\nRUN boom\nRUN never\n").steps, dir),
      (error: unknown) => error instanceof BuildError && error.message.includes("17"),
    );
    assert.deepEqual(vm.commands, ["ok-one", "boom"], "the step after the failure must not run");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}


async function testGlobDoesNotMatchDotfiles(): Promise<void> {
  // `COPY * /app` must not ship `.env`. The hand-rolled matcher turned `*`
  // into `.*`, which matches a leading dot — so TypeScript copied dotfiles
  // where Python (glob) and Docker do not. Same Dockerfile, two images.
  const dir = makeBuildContext();
  fs.writeFileSync(nodePath.join(dir, ".env"), "AWS_SECRET=hunter2\n");
  try {
    const vm = new FakeBuildVM();
    await applySteps(vm, parseDockerfile("FROM x\nCOPY * /app/\n").steps, dir);
    const synced = vm.calls.filter((c) => c.kind === "sync").map((c) => c.a);
    assert.ok(!synced.some((p) => p.endsWith("/.env")), `dotfile copied: ${synced.join(", ")}`);
    assert.ok(synced.some((p) => p.endsWith("/app.js")), synced.join(", "));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testDockerignoreExcludesFromACopiedDirectory(): Promise<void> {
  // `COPY . /app` shipped .git and .env. actions/checkout writes a
  // GITHUB_TOKEN into .git/config, so this leaked CI credentials.
  const dir = makeBuildContext();
  fs.writeFileSync(nodePath.join(dir, ".dockerignore"), ".git\nsecrets.env\n");
  fs.writeFileSync(nodePath.join(dir, "secrets.env"), "TOKEN=hunter2\n");
  fs.mkdirSync(nodePath.join(dir, ".git"));
  fs.writeFileSync(nodePath.join(dir, ".git", "config"), "[remote]\n");
  try {
    const vm = new FakeBuildVM();
    await applySteps(vm, parseDockerfile("FROM x\nCOPY . /app\n").steps, dir);
    const ignore = vm.ignores.at(-1)!;
    assert.ok(ignore(".git/config"), "an ignored directory's contents must be excluded");
    assert.ok(ignore("secrets.env"));
    assert.ok(!ignore("app.js"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testEnvAndWorkdirAfterUserAreNotWrapped(): Promise<void> {
  // `su -c` is a fresh process, so wrapping an export or a cd threw the state
  // away: ENV after USER silently did nothing at all.
  const dir = makeBuildContext();
  try {
    const vm = new FakeBuildVM();
    await applySteps(
      vm,
      parseDockerfile("FROM x\nUSER app\nENV AFTER=1\nWORKDIR /srv\nRUN hi\n").steps,
      dir,
    );
    assert.deepEqual(vm.commands, [
      "export AFTER=1",
      "mkdir -p /srv && cd /srv",
      "su -p app -c hi",
    ]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testEnvWithAVariableExpands(): Promise<void> {
  // `ENV PATH=/opt/bin:$PATH` set the LITERAL string when single-quoted.
  const dir = makeBuildContext();
  try {
    const vm = new FakeBuildVM();
    await applySteps(vm, parseDockerfile("FROM x\nENV PATH=/opt/bin:$PATH\n").steps, dir);
    assert.deepEqual(vm.commands, ['export PATH="/opt/bin:$PATH"']);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

await testGlobDoesNotMatchDotfiles();
await testDockerignoreExcludesFromACopiedDirectory();
await testEnvAndWorkdirAfterUserAreNotWrapped();
await testEnvWithAVariableExpands();


async function testCopyPreservesTheExecutableBit(): Promise<void> {
  const dir = makeBuildContext();
  fs.writeFileSync(nodePath.join(dir, "entrypoint.sh"), "#!/bin/sh\necho hi\n", { mode: 0o755 });
  try {
    const vm = new FakeBuildVM();
    await applySteps(vm, parseDockerfile("FROM x\nCOPY entrypoint.sh /app/e.sh\n").steps, dir);
    assert.ok(
      vm.commands.some((c) => c.includes("chmod") && c.includes("+x")),
      vm.commands.join(", "),
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testCopyLeavesAPlainFileUnexecutable(): Promise<void> {
  const dir = makeBuildContext();
  try {
    const vm = new FakeBuildVM();
    await applySteps(vm, parseDockerfile("FROM x\nCOPY app.js /app/app.js\n").steps, dir);
    assert.ok(!vm.commands.some((c) => c.includes("chmod")), vm.commands.join(", "));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testMultipleDirectorySourcesMergeIntoTheDestination(): Promise<void> {
  const dir = makeBuildContext();
  for (const name of ["x", "y"]) {
    fs.mkdirSync(nodePath.join(dir, name));
    fs.writeFileSync(nodePath.join(dir, name, `${name}.txt`), name);
  }
  try {
    const vm = new FakeBuildVM();
    await applySteps(vm, parseDockerfile("FROM x\nCOPY x y /dest/\n").steps, dir);
    const targets = vm.calls.filter((c) => c.kind === "syncDir").map((c) => c.b);
    assert.deepEqual(targets, ["/dest", "/dest"], JSON.stringify(targets));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testAddChecksumMismatchFailsTheBuild(): Promise<void> {
  const dir = makeBuildContext();
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(new Uint8Array([1, 2, 3]))) as typeof fetch;
  try {
    const wrong = `sha256:${"0".repeat(64)}`;
    const steps = parseDockerfile(
      `FROM x\nADD --checksum=${wrong} https://example.com/f /f\n`,
    ).steps;
    await assert.rejects(() => applySteps(new FakeBuildVM(), steps, dir), /checksum/);
  } finally {
    globalThis.fetch = realFetch;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testAddChecksumMatchIsAccepted(): Promise<void> {
  const dir = makeBuildContext();
  const payload = new Uint8Array([1, 2, 3]);
  const digest = createHash("sha256").update(payload).digest("hex");
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(payload)) as typeof fetch;
  try {
    const steps = parseDockerfile(
      `FROM x\nADD --checksum=sha256:${digest} https://example.com/f /f\n`,
    ).steps;
    const vm = new FakeBuildVM();
    await applySteps(vm, steps, dir);
    assert.ok(vm.calls.some((c) => c.kind === "sync" && c.a.endsWith("/f")), JSON.stringify(vm.calls));
  } finally {
    globalThis.fetch = realFetch;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

await testCopyPreservesTheExecutableBit();
await testCopyLeavesAPlainFileUnexecutable();
await testMultipleDirectorySourcesMergeIntoTheDestination();
await testAddChecksumMismatchFailsTheBuild();
await testAddChecksumMatchIsAccepted();

await testAddUrlIsFetchedByTheClient();
await testAFailingRunAbortsTheBuild();
async function testBuildStepsInheritTheForkQueueingWindow(): Promise<void> {
  const dir = makeBuildContext();
  const vm = new FakeBuildVM();
  await applySteps(vm, parseDockerfile("FROM x\nRUN one\nRUN two\n").steps, dir, {
    queueingTimeout: 900,
  });
  assert.ok(vm.commands.length > 0, "expected RUN commands");
  for (const options of vm.runOptions) {
    assert.equal(options?.queueing_timeout, 900);
  }
}

async function testBuildStepsOmitTheWindowWhenTheForkHadNone(): Promise<void> {
  const dir = makeBuildContext();
  const vm = new FakeBuildVM();
  await applySteps(vm, parseDockerfile("FROM x\nRUN one\n").steps, dir);
  for (const options of vm.runOptions) {
    assert.equal(options?.queueing_timeout, undefined);
  }
}

async function testForkDockerfileGivesBuildStepsTheQueueingWindow(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    { vm_id: "vm_q", owner_org_id: "o", created_at: "now", public: false, state: "idle", sessions: [] },
  );
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_q/runs",
    200,
    { stdout: "", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8", exit_code: 0 },
  );

  await client(fetch).fork({ dockerfile: "FROM ubuntu\nRUN echo hi\n", queueing_timeout: 900 });

  const run = fetch.calls.find((c) => c.url.endsWith("/runs"));
  assert.equal(JSON.parse(run!.body!).queueing_timeout, 900);
}

async function testAFailedDockerfileBuildDeletesTheVm(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/fork",
    200,
    { vm_id: "vm_boom", owner_org_id: "o", created_at: "now", public: false, state: "idle", sessions: [] },
  );
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://test.invalid/api/v1/vms/vm_boom/runs",
    200,
    { stdout: "", stdout_encoding: "utf-8", stderr: "nope", stderr_encoding: "utf-8", exit_code: 1 },
  );
  fetch.addJson(
    (method, url) => method === "DELETE" && url === "https://test.invalid/api/v1/vms/vm_boom",
    200,
    { deleted: true },
  );

  await assert.rejects(() => client(fetch).fork({ dockerfile: "FROM ubuntu\nRUN boom\n" }));

  assert.ok(
    fetch.calls.some((c) => c.method === "DELETE" && c.url.endsWith("/v1/vms/vm_boom")),
    "a failed build must delete the VM it created",
  );
}

testDockerfileParsingBasics();
testDockerfileRefusalsAreNamed();
testDockerfileShell();
await testBuildAppliesShellStateInOrder();
await testUserWrapsOnlyLaterRuns();
await testCopyResolvesAgainstTheContext();
await testCopyRefusesToEscapeTheContext();
await testCopyIsNotWrappedInTheUserShell();
await testBuildStepsInheritTheForkQueueingWindow();
await testBuildStepsOmitTheWindowWhenTheForkHadNone();
await testForkDockerfileGivesBuildStepsTheQueueingWindow();
await testAFailedDockerfileBuildDeletesTheVm();
