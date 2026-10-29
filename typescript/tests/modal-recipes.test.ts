import assert from "node:assert/strict";
import { test } from "node:test";
import { Arker, VM, type ForkOptions } from "../src/index.js";
import { ModalClient } from "../src/modal.js";
import { ModalAppRecipe, registryImage, buildNativeImage, recipeService, creationDefaults } from "../src/compat/modal-recipes.js";
import { preserveEnvironment } from "./provider-fixtures.js";

test("local app and registry recipes fork without native authentication", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  delete process.env.MODAL_TOKEN_ID;
  delete process.env.MODAL_TOKEN_SECRET;
  const requests: ForkOptions[] = [];
  t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => {
    requests.push(request); return { id: "owned" } as VM;
  });
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", source: "unused" } });
  const app = await client.apps.fromName("same-workload", { createIfMissing: true });
  const image = await client.images.fromRegistry("python:3.12-slim").build(app);
  const box = await client.sandboxes.create(app, image);
  assert.equal(box.sandboxId, "owned");
  assert.equal(requests[0]!.image, "python:3.12-slim");
  assert.deepEqual(requests[0]!.metadata, { "computesdk-modal-app": { name: "same-workload" } });
  assert.equal("appId" in app, false);
});

test("opaque handles and unknown app ownership reject before fork", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const fork = t.mock.method(Arker.prototype, "fork", async () => { throw new Error("must not fork"); });
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", source: "unused" } });
  await assert.rejects(client.sandboxes.create({}, {}), /opaque/);
  const app = await client.apps.fromName("existing");
  await assert.rejects(client.sandboxes.create(app, client.images.fromRegistry("alpine")), /createIfMissing/);
  assert.equal(fork.mock.callCount(), 0);
});

test("recipes preserve native build ownership and error identity", async () => {
  const app = new ModalAppRecipe("native", { createIfMissing: true });
  const nativeApp = { appId: "ap-native" };
  const failure = new Error("native build failure");
  const calls: unknown[] = [];
  const client = {
    apps: { fromName: async (...args: unknown[]) => { calls.push(args); return nativeApp; } },
    images: { fromRegistry: (...args: unknown[]) => { calls.push(args); return { build: async (owner: unknown) => {
      assert.equal(owner, nativeApp); throw failure;
    } }; } },
  } as any;
  const image = registryImage(client, "private/image");
  await image.build(app);
  assert.deepEqual(calls, [["private/image", undefined]]);
  assert.equal(await app.native(client), nativeApp);
  await assert.rejects(buildNativeImage(image, nativeApp as any), error => error === failure);
  assert.deepEqual(calls, [["private/image", undefined], ["native", { createIfMissing: true }]]);
});

test("unrelated native service methods keep their receiver", () => {
  const native = { value: "native", get() { return this.value; } };
  let constructed = 0;
  const service = recipeService(() => { constructed++; return native; }, { recipe: () => "local" });
  assert.equal(service.recipe(), "local");
  assert.equal(constructed, 0);
  assert.equal(service.get(), "native");
});

test("creation secrets survive reconnect without entering metadata or commands", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const { GuestProcesses } = await import("../src/compat/guest-process.js");
  const contents = new Map<string, string>();
  const commands: string[] = [];
  let request: ForkOptions | undefined;
  const vm = {
    id: "owned",
    metadata: { "computesdk-modal-defaults": true },
    run: async (command: string) => { commands.push(command); return { type: "completed", stdout: "", stderr: "", exit_code: 0 }; },
    sync: async (path: string, data?: string) => {
      if (data !== undefined) { contents.set(path, data); return; }
      return new TextEncoder().encode(contents.get(path));
    },
  } as unknown as VM;
  t.mock.method(Arker.prototype, "fork", async (value: ForkOptions) => { request = value; return vm; });
  t.mock.method(Arker.prototype, "getVm", async () => vm);
  const spawn = t.mock.method(GuestProcesses.prototype, "exec", async () => ({ pid: "owned-process" }) as any);
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", source: "fixture" } });
  const secret = await client.secrets.fromObject({ TOKEN: "synthetic-secret", ORDER: "secret" });
  await client.sandboxes.create({ secrets: [secret], env: { ORDER: "explicit" }, workdir: "/tmp" });
  const connected = await client.sandboxes.fromId("owned");
  await connected.exec(["true"], { env: { ORDER: "exec" } });
  assert.deepEqual(spawn.mock.calls[0]!.arguments[0]!.env, {});
  const control = JSON.parse([...contents.entries()].find(([path]) => path.includes("/exec-"))![1]);
  assert.deepEqual(control.env, { ORDER: "exec" });
  assert.equal(control.defaults, true);
  assert.equal(control.workdir, null);
  assert.deepEqual(JSON.parse(contents.get("/root/.arker-modal/defaults.json")!), { env: { TOKEN: "synthetic-secret", ORDER: "explicit" }, workdir: "/tmp" });
  assert.equal(spawn.mock.calls[0]!.arguments[0]!.workingDir, "/");
  assert.equal(JSON.stringify(request).includes("synthetic-secret"), false);
  assert.equal(commands.join("\n").includes("synthetic-secret"), false);
  assert.equal(contents.size, 2);
});

for (const cleanupFails of [false, true]) {
  test(`failed secret persistence deletes its owned VM (cleanupFails=${cleanupFails})`, async t => {
    preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
    delete process.env.COMPUTESDK_ROUTER_CONFIG;
    const failure = new Error("sync failed");
    const cleanup = new Error("delete failed");
    const vm = {
      id: "owned",
      run: async () => ({ type: "completed", stdout: "", stderr: "", exit_code: 0 }),
      sync: async () => { throw failure; },
    } as unknown as VM;
    t.mock.method(Arker.prototype, "fork", async () => vm);
    const remove = t.mock.method(VM.prototype, "delete", async function (this: VM) {
      assert.equal(this.id, "owned");
      if (cleanupFails) throw cleanup;
    });
    const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", source: "fixture" } });
    const secret = await client.secrets.fromObject({ TOKEN: "synthetic" });
    await assert.rejects(client.sandboxes.create({ secrets: [secret] }), error =>
      { if (error instanceof AggregateError) assert.deepEqual(error.errors, [failure, cleanup]);
        return cleanupFails ? error instanceof AggregateError : error === failure; });
    assert.equal(remove.mock.callCount(), 1);
  });
}

test("named volumes cannot trigger native authentication on Arker creation", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  delete process.env.MODAL_TOKEN_ID;
  delete process.env.MODAL_TOKEN_SECRET;
  const fork = t.mock.method(Arker.prototype, "fork", async () => { throw new Error("must not fork"); });
  const client = new ModalClient({ arker: { apiKey: "synthetic", source: "fixture" } });
  const volume = await client.volumes.fromName("native-volume", { createIfMissing: true });
  assert.equal("volumeId" in volume, false);
  await assert.rejects(client.sandboxes.create({ volumes: { "/data": volume } }), /volumes/);
  assert.equal(fork.mock.callCount(), 0);
});


test("environment dictionaries preserve prototype-named variables without prototype mutation", () => {
  const env = JSON.parse('{"__proto__":"preserved","constructor":"value"}');
  const defaults = creationDefaults({ env });
  assert.equal(Object.getPrototypeOf(defaults.env), null);
  assert.equal(defaults.env.__proto__, "preserved");
  assert.equal(defaults.env.constructor, "value");
  assert.throws(() => creationDefaults({ env: JSON.parse('{"__proto__":{"injected":"value"}}') }), /Invalid Modal environment/);
  assert.equal(({} as Record<string, unknown>).injected, undefined);
});

test("actual process wire payload contains no creation or exec secrets", async t => {
  preserveEnvironment(t, "COMPUTESDK_ROUTER_CONFIG");
  delete process.env.COMPUTESDK_ROUTER_CONFIG;
  const commands: string[] = [], contents = new Map<string, string>();
  contents.set("/root/.arker-modal/defaults.json", JSON.stringify({ env: { TOKEN: "creation-secret" }, workdir: "/tmp" }));
  const stop = new Error("captured supervisor command");
  const vm = {
    id: "owned", metadata: { "computesdk-modal-defaults": true },
    createSession: async () => ({ session_id: "session" }),
    run: async (command: string) => {
      commands.push(command);
      if (command.includes(" supervise ")) throw stop;
      return { type: "completed", stdout: "", stderr: "", exit_code: 0 };
    },
    sync: async (path: string, data?: string) => {
      if (data !== undefined) { contents.set(path, data); return; }
      return new TextEncoder().encode(contents.get(path));
    },
  } as unknown as VM;
  t.mock.method(Arker.prototype, "getVm", async () => vm);
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", source: "unused" } });
  try {
    const box = await client.sandboxes.fromId("owned");
    await assert.rejects(box.exec(["true"], { env: { EXEC_TOKEN: "exec-secret" } }), error => error === stop);
    const wire = commands.find(command => command.includes(" supervise "))!;
    assert.ok(wire);
    const encoded = wire.slice(wire.lastIndexOf(" supervise ") + 11).replaceAll("'", "");
    const payload = Buffer.from(encoded, "base64").toString();
    for (const secret of ["creation-secret", "exec-secret"]) {
      assert.equal(wire.includes(secret), false);
      assert.equal(payload.includes(secret), false);
    }
  } finally { client.close(); }
});


test("registry credentials enter only the image pull request", async t => {
  const requests: ForkOptions[] = [];
  t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => {
    requests.push(request); return { id: "owned" } as VM;
  });
  const client = new ModalClient({ arker: { apiKey: "synthetic", baseUrl: "https://fixture.invalid", source: "unused" } });
  try {
    const app = await client.apps.fromName("registry-test", { createIfMissing: true });
    const secret = await client.secrets.fromObject({ REGISTRY_USERNAME: "fixture-user", REGISTRY_PASSWORD: "fixture-password", OTHER: "not-forwarded" });
    await client.sandboxes.create(app, client.images.fromRegistry("private/image", secret));
    assert.deepEqual(requests[0]!.registry_auth, { username: "fixture-user", password: "fixture-password" });
    assert.deepEqual(requests[0]!.metadata, { "computesdk-modal-app": { name: "registry-test" } });
    await client.sandboxes.create(app, client.images.fromRegistry("public/image"));
    assert.equal(requests[1]!.registry_auth, undefined);
  } finally { client.close(); }
});

test("per-image registry auth preserves configured defaults and rejects before fork", async t => {
  const { createArkerComputeProvider } = await import("../src/compat/arker-provider.js");
  const requests: ForkOptions[] = [];
  t.mock.method(Arker.prototype, "fork", async (request: ForkOptions) => {
    requests.push(request); return { id: "owned" } as VM;
  });
  const configured = { username: "default-user", password: "default-password" };
  const provider = createArkerComputeProvider({ apiKey: "synthetic", baseUrl: "https://fixture.invalid", sourceVmId: "source", registryAuth: configured });
  await provider.sandbox.create();
  await provider.sandbox.create({ image: "image", registryAuth: { username: "image-user", password: "image-password" } });
  await provider.sandbox.create({ image: "image" });
  assert.equal(requests[0]!.registry_auth, undefined);
  assert.deepEqual(requests[1]!.registry_auth, { username: "image-user", password: "image-password" });
  assert.deepEqual(requests[2]!.registry_auth, configured);
  await assert.rejects(provider.sandbox.create({ registryAuth: configured }), /Registry authentication requires an image/);
  await assert.rejects(provider.sandbox.create({ image: "image", registryAuth: { username: 1, password: "invalid" } }), /username\/password strings/);
  assert.equal(requests.length, 3);
  assert.deepEqual(configured, { username: "default-user", password: "default-password" });
});
