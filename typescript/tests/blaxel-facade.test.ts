import assert from "node:assert/strict";
import { test } from "node:test";
import { SandboxInstance } from "../src/blaxel.js";
import { createBlaxelDataPlane } from "../src/compat/blaxel-data-plane.js";

test("facade preserves native component identity and disposes on delete failure", async () => {
  const plane = createBlaxelDataPlane({connectPort: async () => {throw new Error("unexpected I/O");}});
  const box = {provider: "arker", sandboxId: "vm-test", getInstance: () => ({name: "test", delete: async () => {throw new Error("delete failed");}}), destroy: async () => {assert.fail("delete result discarded");}};
  const facade = Reflect.construct(SandboxInstance, [box, undefined, undefined, plane]) as SandboxInstance;
  assert.equal(facade.fs, plane.fs);
  assert.equal(facade.process, plane.process);
  await assert.rejects(facade.delete(), /delete failed/);
  await assert.rejects(facade.fs.read("/file"), /closed/);
});

test("bootstrap retains both initialization and staging cleanup errors", async () => {
  const { bootstrapBlaxel } = await import("../src/compat/blaxel-bootstrap.js");
  const primary = new Error("ensure failed"), cleanup = new Error("cleanup failed");
  const vm = {
    id: "test", baseUrl: "https://custom.example/api",
    _client: { _request: async () => ({customer_paused: false}) },
    refresh: async () => ({policies: {rules: {}}}), sync: async () => {},
    run: async (command: string) => {
      if (command.includes("prepare")) return {exitCode: 0, stdout: '{"verified":true,"arch":"amd64"}'};
      throw command.startsWith("rm ") ? cleanup : primary;
    },
  };
  await assert.rejects(bootstrapBlaxel(vm as never), error => {
    assert.ok(error instanceof AggregateError);
    assert.deepEqual(error.errors, [primary, cleanup]);
    return true;
  });
});

test("failed creation retains initialization, cleanup and resource identity", async t => {
  const { setCompatComputeFactoryForTests } = await import("../src/compat/runtime.js");
  const primary = new Error("initialization failed"), cleanup = new Error("delete failed");
  const vm = {_client: {_request: async () => {throw primary;}}};
  const box = {provider: "arker", sandboxId: "vm-test", getInstance: () => vm,
    destroy: async () => {throw cleanup;}};
  setCompatComputeFactoryForTests(() => ({sandbox: {create: async () => box}}) as never);
  t.after(() => setCompatComputeFactoryForTests());
  await assert.rejects(SandboxInstance.create(), error => {
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [primary, cleanup]);
    assert.equal(Reflect.get(error, "sandboxId"), "vm-test");
    return true;
  });
});

test("get-or-create initialization failure preserves the authoritative existing VM", async t => {
  const { setCompatComputeFactoryForTests } = await import("../src/compat/runtime.js");
  const failure = new Error("bootstrap refused");
  const box = {provider: "arker", sandboxId: "existing", getInstance: () => ({_client: {_request: async () => {throw failure;}}}),
    destroy: async () => {assert.fail("borrowed VM deleted");}};
  setCompatComputeFactoryForTests(() => ({sandbox: {create: async () => box}}) as never);
  t.after(() => setCompatComputeFactoryForTests());
  await assert.rejects(SandboxInstance.createIfNotExists({name: "named"}), error => error === failure);
});

test("named fork uses global transport and maps only duplicate conflicts", async t => {
  const { createServer } = await import("node:http");
  const { listen, closeServer, preserveEnvironment } = await import("./provider-fixtures.js");
  const { Arker, ArkerError } = await import("../src/index.js");
  const { namedBlaxelFork } = await import("../src/compat/blaxel-create.js");
  preserveEnvironment(t, "ARKER_BASE_URL", "ARKER_CONTROL_BASE_URL");
  delete process.env.ARKER_BASE_URL;
  delete process.env.ARKER_CONTROL_BASE_URL;
  const requests: Array<{url?: string; method?: string; headers: Record<string, unknown>; body: unknown}> = [];
  let status = 200, code = "conflict";
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    requests.push({url: request.url, method: request.method, headers: request.headers, body: JSON.parse(body)});
    response.writeHead(status, {"content-type": "application/json"});
    response.end(JSON.stringify(status === 200 ? {
      vm_id: "winner", name: "named", description: null, owner_org_id: "org", created_at: "2026-01-01T00:00:00Z",
      public: false, state: "idle", sessions: [], network: {}, resources: {},
    } : {error: {code, message: "refused", timestamp: "2026-01-01T00:00:00Z", request_id: "req-test",
      request: {kind: "matched", operation_id: "fork"}, details: {resource: "vm"}}}));
  });
  t.after(() => closeServer(server));
  const {port} = await listen(server);
  const config = {apiKey: "ark_test", controlBaseUrl: `http://127.0.0.1:${port}/api`, region: "region-one", provider: "aws" as const};
  const client = new Arker(config);
  const request = {name: "named", source_vm_name: "source"};
  for (const getOrCreate of [false, true]) {
    const vm = await namedBlaxelFork(config, getOrCreate)(client, request);
    assert.equal(vm.id, "winner");
    assert.equal(vm.baseUrl, config.controlBaseUrl);
    const received = requests.at(-1)!;
    assert.equal(received.url, "/api/v1/named-fork");
    assert.equal(received.method, "POST");
    assert.equal(received.headers["arker-vm-name-mode"], getOrCreate ? "get_or_create" : "create");
    assert.equal(received.headers["arker-vm-name"], "named");
    assert.equal(received.headers["x-arker-vm-region"], "region-one");
    assert.equal(received.headers["x-arker-vm-provider"], "aws");
    assert.equal(received.headers["idempotency-key"], undefined);
    assert.deepEqual(received.body, request);
  }
  status = 409;
  await assert.rejects(namedBlaxelFork(config)(client, request), error => {
    assert.equal(Reflect.get(error as object, "status_code"), 409);
    assert.equal(Reflect.get(error as object, "code"), "SANDBOX_ALREADY_EXISTS");
    return true;
  });
  status = 403; code = "forbidden";
  await assert.rejects(namedBlaxelFork(config)(client, request), error => error instanceof ArkerError && error.status === 403);
  status = 404; code = "not_found";
  for (const getOrCreate of [false, true]) {
    const before = requests.length;
    await assert.rejects(namedBlaxelFork(config, getOrCreate)(client, request),
      error => error instanceof ArkerError && error.status === 404);
    assert.equal(requests.length, before + 1);
    assert.equal(requests.at(-1)!.url, "/api/v1/named-fork");
  }
  const count = requests.length;
  await assert.rejects(namedBlaxelFork({baseUrl: "https://regional.example/api"})(client, request), /controlBaseUrl/);
  await assert.rejects(namedBlaxelFork(config)(client, {...request, name: "Invalid Name"}), error => {
    assert.equal(Reflect.get(error as object, "status_code"), 400);
    return true;
  });
  assert.equal(requests.length, count);
});

for (const getOrCreate of [false, true]) {
  test(`lost committed response ${getOrCreate ? "reconciles get-or-create" : "leaves strict outcome unknown"}`, async () => {
    const {Arker, ArkerError} = await import("../src/index.js");
    const {namedBlaxelFork} = await import("../src/compat/blaxel-create.js");
    const requests: Request[] = [];
    const config = {controlBaseUrl: "https://control.example/api"};
    const client = new Arker({...config, apiKey: "ark_test", retry: {attempts: 3, baseDelayMs: 0, jitterMs: 0},
      fetch: (async (input, init) => {
        requests.push(new Request(input, init));
        if (requests.length === 1) throw new TypeError("committed response lost");
        return Response.json({vm_id: "committed", name: "named", description: null, owner_org_id: "org",
          created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], network: {}, resources: {}});
      }) as typeof fetch});
    const operation = namedBlaxelFork(config, getOrCreate)(client, {name: "named", source_vm_name: "source"});
    if (getOrCreate) {
      assert.equal((await operation).id, "committed");
      assert.equal(requests.length, 2);
    } else {
      await assert.rejects(operation, error => error instanceof ArkerError && error.status === 0 && /outcome is unknown/.test(error.message));
      assert.equal(requests.length, 1);
    }
    for (const request of requests) {
      assert.equal(request.method, "POST");
      assert.equal(request.headers.get("Idempotency-Key"), null);
    }
  });
}

test("named routing never falls back on authority errors or caches an unconfirmed owner", async t => {
  const {mkdtemp, writeFile, readFile, rm} = await import("node:fs/promises");
  const {tmpdir} = await import("node:os");
  const {join} = await import("node:path");
  const {createCompatRouter} = await import("../src/compat/runtime.js");
  const {namedBlaxelFork} = await import("../src/compat/blaxel-create.js");
  const {blaxelControlCall} = await import("../src/compat/blaxel-errors.js");
  const directory = await mkdtemp(join(tmpdir(), "blaxel-atomic-"));
  t.after(() => rm(directory, {recursive: true, force: true}));
  const path = join(directory, "router.json"), cache = join(directory, "cache.json");
  await writeFile(path, JSON.stringify({version: 1, create: {strategy: "priority", order: ["arker", "blaxel"]},
    existing: {strategy: "priority", order: ["arker", "blaxel"]}, cache: {path: cache}}));
  let response: () => Promise<Response> = async () => Response.json({error: {code: "forbidden", message: "denied"}}, {status: 403});
  const config = {apiKey: "ark_test", source: "source", controlBaseUrl: "https://control.example/api",
    fetch: (() => response()) as typeof fetch};
  const router = createCompatRouter({arker: config, arkerFork: (client, request) => blaxelControlCall("create", () => namedBlaxelFork(config, true)(client, request)), routerConfigPath: path,
    fallbackProvider: {name: "blaxel", sandbox: {create: async () => assert.fail("authority failure fell back"),
      getById: async () => assert.fail("unexpected lookup"), destroy: async () => assert.fail("unproven deletion")}}});
  for (const status of [401,403]) {
    response = async () => Response.json({error: {code: status === 401 ? "unauthorized" : "forbidden", message: "denied"}}, {status});
    await assert.rejects(router.instances!.create({name: "named"}), error => typeof error === "object" && error !== null && Reflect.get(error, "status_code") === status);
    await assert.rejects(readFile(cache), {code: "ENOENT"});
  }
  let entered!: () => void, release!: (value: Response) => void;
  const started = new Promise<void>(resolve => {entered = resolve;});
  const pending = new Promise<Response>(resolve => {release = resolve;});
  response = () => {entered(); return pending;};
  const create = router.instances!.create({name: "named"});
  await started;
  await assert.rejects(readFile(cache), {code: "ENOENT"});
  release(Response.json({vm_id: "winner", name: "named", description: null, owner_org_id: "org",
    created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], network: {}, resources: {}}));
  assert.equal((await create).sandboxId, "winner");
  assert.equal(JSON.parse(await readFile(cache, "utf8")).entries.winner.provider, "arker");
});

test("named fork preserves the canonical policy read-to-write transformation", async () => {
  const {Arker} = await import("../src/index.js");
  const {namedBlaxelFork} = await import("../src/compat/blaxel-create.js");
  const policy = {hostname: "response-only", mitm_domains: ["response.example"], warnings: [],
    outbound_guards: {floor: {type: "outbound_guard" as const, allow_cidrs: ["10.0.0.0/8"]},
      removed: {type: "outbound_guard" as const, allow_cidrs: []}},
    rules: {removed: null, custom: {type: "schedule" as const, after_created: 60, action: "delete" as const}},
    secrets: {remove: null, token: "synthetic"}};
  const original = structuredClone(policy);
  const calls: Array<{headers: Headers; body: {policies: unknown}}> = [];
  const config = {controlBaseUrl: "https://control.example/api", provider: "aws" as const, region: "region-one"};
  const client = new Arker({...config, apiKey: "ark_test", retry: false, fetch: (async (input, init) => {
    const request = new Request(input, init);
    calls.push({headers: request.headers, body: await request.json() as {policies: unknown}});
    return Response.json({vm_id: "winner", name: "named", description: null, owner_org_id: "org",
      created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], network: {}, resources: {}});
  }) as typeof fetch});
  const request = {name: "named", source_vm_name: "source", policies: policy};
  await client.fork({...request, idempotency: true});
  await namedBlaxelFork(config)(client, request);
  const expected = {rules: {floor: policy.outbound_guards.floor, ...policy.rules}, secrets: policy.secrets};
  assert.deepEqual(calls[0]!.body.policies, expected);
  assert.deepEqual(calls[1]!.body.policies, expected);
  assert(calls[0]!.headers.get("Idempotency-Key"));
  assert.equal(calls[1]!.headers.get("Idempotency-Key"), null);
  assert.equal(calls[1]!.headers.get("Arker-VM-Name-Mode"), "create");
  assert.deepEqual(policy, original);
});


test("Blaxel control boundaries preserve native shapes without hiding unknown outcomes", async () => {
  const {ArkerError} = await import("../src/index.js");
  const {blaxelControlError} = await import("../src/compat/blaxel-errors.js");
  for (const status of [401, 403]) {
    const original = new ArkerError("forbidden", "controlled refusal", status);
    for (const operation of ["create", "get", "delete"] as const) {
      const mapped = blaxelControlError(original, operation);
      assert.deepEqual(mapped, operation === "create"
        ? {code: "forbidden", message: "controlled refusal", status_code: status}
        : {code: status, error: "controlled refusal"});
      assert.equal(Reflect.get(mapped as object, "cause"), original);
    }
  }
  for (const [status, code] of [[0, "unavailable"], [408, "unavailable"], [504, "unavailable"], [409, "invalid_state"]] as const) {
    const original = new ArkerError(code, "unknown outcome", status);
    assert.equal(blaxelControlError(original, "create"), original);
  }
  const native = {code: "UNAUTHORIZED", status_code: 403, message: "native"};
  assert.equal(blaxelControlError(native, "create"), native);
});

test("Blaxel instance delete reports authoritative absence without preliminary lookup", async () => {
  let deleted = 0;
  const vm = {name: "missing", delete: async () => {deleted++; return {deleted: false};}};
  const plane = createBlaxelDataPlane({connectPort: async () => {assert.fail("unexpected guest activation");}});
  const box = {provider: "arker", sandboxId: "missing", getInstance: () => vm,
    destroy: async () => assert.fail("must retain atomic deletion result")};
  const facade = Reflect.construct(SandboxInstance, [box, undefined, undefined, plane]) as SandboxInstance;
  await assert.rejects(facade.delete(), error => {
    assert.deepEqual(error, {code: 404, error: "Sandbox not found"}); return true;
  });
  assert.equal(deleted, 1);
  await assert.rejects(plane.fs.read("/file"), /closed/);
});

test("name validation retains native types, length and messages before requests", async () => {
  const { namedBlaxelFork, validateBlaxelName } = await import("../src/compat/blaxel-create.js");
  const { Arker } = await import("../src/index.js");
  let calls = 0;
  const client = new Arker({apiKey: "ark_test", fetch: (async () => { calls++; throw Error("unexpected request"); }) as typeof fetch});
  const cases: Array<[unknown, string]> = [
    [123, "Failed to parse sandbox body"], [0, "Failed to parse sandbox body"], [false, "Failed to parse sandbox body"],
    ["a".repeat(50), "Invalid sandbox configuration: metadata.name is too long (maximum 49 characters)"],
    ["Upper", "Invalid sandbox configuration: metadata.name must contain only lower case alphanumeric characters and hyphens"],
    ["under_score", "Invalid sandbox configuration: metadata.name must contain only lower case alphanumeric characters and hyphens"],
  ];
  for (const [name, message] of cases) {
    await assert.rejects(namedBlaxelFork()(client, {name} as never), error => {
      assert.deepEqual(error, {code: "VALIDATION_FAILED", message, status_code: 400}); return true;
    });
  }
  validateBlaxelName("a".repeat(49));
  assert.equal(calls, 0);
});

test("facade rejects falsy non-string names before generic option preparation", async t => {
  const {mkdtemp, writeFile, rm} = await import("node:fs/promises");
  const {tmpdir} = await import("node:os");
  const {join} = await import("node:path");
  const {SandboxInstance} = await import("../src/blaxel.js");
  const directory = await mkdtemp(join(tmpdir(), "blaxel-invalid-name-"));
  t.after(() => rm(directory, {recursive: true, force: true}));
  const path = join(directory, "router.json");
  await writeFile(path, JSON.stringify({version: 1, create: {strategy: "priority", order: ["arker"]},
    existing: {strategy: "priority", order: ["arker"]}, cache: {path: join(directory, "cache.json")}}));
  for (const name of [0, false, 123]) {
    await assert.rejects(SandboxInstance.create({name} as never, {routerConfigPath: path,
      arker: {apiKey: "ark_test", source: "source", controlBaseUrl: "https://control.example/api",
        fetch: (async () => {assert.fail("invalid name reached HTTP");}) as typeof fetch}}), error => {
      assert.deepEqual(error, {code: "VALIDATION_FAILED", message: "Failed to parse sandbox body", status_code: 400});
      return true;
    });
  }
});

test("Arker metadata preserves authoritative birth and stored labels", () => {
  const plane = createBlaxelDataPlane({connectPort: async () => {throw new Error("unexpected I/O");}});
  const vm = {name: "named", created_at: "2026-01-01T00:00:00Z", metadata: {arker_blaxel: {version: 1, labels: {purpose: "test"}}}};
  const box = {provider: "arker", sandboxId: "vm-test", getInstance: () => vm};
  try {
    const facade = Reflect.construct(SandboxInstance, [box, undefined, undefined, plane]);
    assert.equal(facade.metadata.name, "named");
    assert.equal(facade.metadata.createdAt, vm.created_at);
    assert.deepEqual(Object.entries(facade.metadata.labels), [["purpose", "test"]]);
  } finally { plane.close(); }
});


test("Blaxel labels have explicit creation metadata without inherited source labels", async () => {
  const { blaxelCreationMetadata, blaxelSandboxMetadata } = await import("../src/compat/blaxel-create.js");
  assert.deepEqual(blaxelCreationMetadata(undefined), {arker_blaxel: {version: 1, labels: {}}});
  assert.deepEqual(blaxelCreationMetadata({purpose: "test"}), {arker_blaxel: {version: 1, labels: {purpose: "test"}}});
  assert.throws(() => blaxelCreationMetadata({purpose: 3}), /string mapping/);
  const { Arker } = await import("../src/index.js");
  const vm = new Arker({apiKey: "test", baseUrl: "https://custom.example/api"}).vm("vm-test");
  assert.deepEqual(blaxelSandboxMetadata(vm), {name: "vm-test"});
});

test("atomic winner, loser and refreshed VM retain authoritative labels", async t => {
  const { createServer } = await import("node:http");
  const { listen, closeServer, preserveEnvironment } = await import("./provider-fixtures.js");
  const { Arker } = await import("../src/index.js");
  const { namedBlaxelFork, blaxelCreationMetadata } = await import("../src/compat/blaxel-create.js");
  preserveEnvironment(t, "ARKER_BASE_URL", "ARKER_CONTROL_BASE_URL");
  delete process.env.ARKER_BASE_URL;
  delete process.env.ARKER_CONTROL_BASE_URL;
  let winner: unknown;
  const requests: unknown[] = [];
  const server = createServer(async (request, response) => {
    if (request.method === "POST") {
      let body = "";
      for await (const chunk of request) body += chunk;
      const metadata = JSON.parse(body).metadata;
      requests.push(metadata);
      winner ??= metadata;
    }
    response.writeHead(200, {"content-type": "application/json"});
    response.end(JSON.stringify({vm_id: "winner", name: "named", description: null,
      owner_org_id: "org", created_at: "2026-01-01T00:00:00Z", public: false,
      state: "idle", sessions: [], network: {}, resources: {}, metadata: winner}));
  });
  t.after(() => closeServer(server));
  const {port} = await listen(server);
  const config = {apiKey: "test", baseUrl: `http://127.0.0.1:${port}/api`, controlBaseUrl: `http://127.0.0.1:${port}/api`};
  const client = new Arker(config);
  const first = await namedBlaxelFork(config, true)(client, {name: "named", source_vm_name: "source",
    metadata: blaxelCreationMetadata({owner: "winner"})});
  const second = await namedBlaxelFork(config, true)(client, {name: "named", source_vm_name: "source",
    metadata: blaxelCreationMetadata({owner: "loser"})});
  const reconnected = await client.vm(first.id).refresh();
  assert.notDeepEqual(requests[0], requests[1]);
  for (const vm of [first, second, reconnected]) {
    const plane = createBlaxelDataPlane({connectPort: async () => {throw new Error("unexpected I/O");}});
    try {
      const facade = Reflect.construct(SandboxInstance,
        [{provider: "arker", sandboxId: vm.id, getInstance: () => vm}, undefined, undefined, plane]);
      assert.equal(facade.metadata.createdAt, "2026-01-01T00:00:00Z");
      assert.deepEqual(Object.entries(facade.metadata.labels), [["owner", "winner"]]);
    } finally { plane.close(); }
  }
});
