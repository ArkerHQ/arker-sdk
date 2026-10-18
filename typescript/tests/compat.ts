import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync, symlinkSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { CommandResult, FileEntry, RunCommandOptions, SandboxInfo, SandboxInterface } from "computesdk";

import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { CommandExitError } from "e2b";
import { Image as DaytonaImage } from "@daytona/sdk";
import { Secret as ModalSecret, CloudBucketMount as ModalBucketMount } from "modal";
import { ArkerError } from "../src/index.js";
import { SandboxInstance as BlaxelSandbox } from "../src/blaxel.js";
import { ModalClient } from "../src/modal.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";
import { createCompatSdk, setCompatComputeFactoryForTests } from "../src/compat/runtime.js";
import { GuestProcesses } from "../src/compat/guest-process.js";

type CapturedComputeConfig = {
  providers: Array<{ name?: string }>;
};

class FakeSandbox implements SandboxInterface {
  readonly instanceId: string;
  readonly sandboxId: string;
  readonly provider: string;
  readonly runCalls: Array<{ command: string; options?: RunCommandOptions }> = [];
  readonly createOptions: unknown[] = [];
  destroyed = false;

  constructor(sandboxId = "fake-arker-vm", provider = "arker", private readonly native?: object) {
    this.sandboxId = sandboxId;
    this.instanceId = sandboxId;
    this.provider = provider;
  }

  getInstance(): object { return this.native ?? this; }

  async runCommand(command: string, options?: RunCommandOptions): Promise<CommandResult> {
    this.runCalls.push({ command, options });
    return {
      stdout: `stdout:${command}`,
      stderr: `stderr:${command}`,
      exitCode: 7,
      durationMs: 3,
    };
  }

  async startProcess(): Promise<any> {
    return { jobId: "fake", pid: null };
  }

  async getInfo(): Promise<SandboxInfo> {
    return {
      id: this.sandboxId,
      provider: this.provider,
      status: "running",
      createdAt: new Date(0),
      timeout: 0,
    };
  }

  async getUrl(options: { port: number; protocol?: string }): Promise<string> {
    return `${options.protocol ?? "https"}://p${options.port}.example.invalid`;
  }

  async destroy(): Promise<void> {
    this.destroyed = true;
  }

  async delete(): Promise<{ deleted: boolean }> {
    const deleted = !this.destroyed;
    this.destroyed = true;
    return { deleted };
  }

  async refresh(): Promise<this> {
    if (this.destroyed) throw new ArkerError("not_found", "VM not found", 404);
    return this;
  }

  readonly filesystem = {
    readFile: async (path: string): Promise<string> => `read:${path}`,
    writeFile: async (path: string, content: string): Promise<void> => {
      this.createOptions.push(["writeFile", path, content]);
    },
    readdir: async (path: string): Promise<FileEntry[]> => [{ name: path, type: "file", size: 1 }],
    mkdir: async (path: string): Promise<void> => {
      this.createOptions.push(["mkdir", path]);
    },
    exists: async (path: string): Promise<boolean> => path.length > 0,
    remove: async (path: string): Promise<void> => {
      this.createOptions.push(["remove", path]);
    },
  };
}

function installFakeCompute(box: FakeSandbox, options: { getByIdResult?: SandboxInterface | null } = {}) {
  let captured: CapturedComputeConfig | undefined;
  const createCalls: unknown[] = [];
  setCompatComputeFactoryForTests((config) => {
    captured = config as CapturedComputeConfig;
    return {
      sandbox: {
        create: async (createOptions?: unknown) => {
          createCalls.push(createOptions);
          return box;
        },
        getById: async () => Object.hasOwn(options, "getByIdResult") ? options.getByIdResult ?? null : box,
        list: async () => [box],
        destroy: async () => {
          await box.destroy();
        },
      },
    } as any;
  });
  return {
    captured: () => {
      assert.ok(captured, "expected compute factory to be called");
      return captured;
    },
    createCalls,
    cleanup: () => setCompatComputeFactoryForTests(),
  };
}

function assertArkerFirst(config: CapturedComputeConfig, fallbackName: string): void {
  void fallbackName;
  assert.deepEqual(config.providers.map((provider) => provider.name), ["arker-compat-router"]);
}

async function testDaytonaSurface(): Promise<void> {
  const box = new FakeSandbox("daytona-box");
  const fake = installFakeCompute(box);
  try {
    const daytona = new Daytona({ apiKey: "daytona-key", arker: { apiKey: "ark-key", baseUrl: "https://arker.invalid/api" } });
    const sandbox = await daytona.create();
    assertArkerFirst(fake.captured(), "daytona");
    assert.equal(sandbox.id, "daytona-box");

    const result = await sandbox.process.exec("echo hi", { cwd: "/workspace", env: { A: "b" }, timeout: 100 });
    assert.equal(result.exitCode, 7);
    assert.equal(result.result, "stdout:echo hi");
    assert.equal(result.stdout, "stdout:echo hi");
    assert.equal(result.stderr, "stderr:echo hi");
    assert.deepEqual(box.runCalls[0]!.options, { cwd: "/workspace", env: { A: "b" }, timeout: 100_000 });
    const executeCommandResult = await sandbox.process.executeCommand("echo compat", "/official", { OFFICIAL: "yes" }, 12);
    assert.equal(executeCommandResult.result, "stdout:echo compat");
    assert.deepEqual(box.runCalls[1]!.options, { cwd: "/official", env: { OFFICIAL: "yes" }, timeout: 12_000 });

    await daytona.create({ image: "ubuntu:24.04", resources: { cpu: 4, memory: 8, disk: 20 } });
    assert.deepEqual((fake.createCalls[1] as any).resources, { cpu: 4, memory: 8, disk: 20 });
    assert.equal((fake.createCalls[1] as any).memory, undefined);

    await daytona.delete(sandbox);
    assert.equal(box.destroyed, true);

    await assert.rejects(
      () => daytona.create({ volumeMounts: [] } as any),
      /unsupported option\(s\) 'volumeMounts'/,
    );
    assert.throws(
      () => (sandbox.process as any).spawn,
      /not supported by the Arker compatibility layer for daytona/,
    );
    assert.throws(
      () => new Daytona({ apiKey: "daytona-key", unknownOption: "unsupported" } as any),
      /unsupported option\(s\) 'unknownOption'/,
    );
  } finally {
    fake.cleanup();
  }
}

async function testNativeProviderExtensionsAreDelegated(): Promise<void> {
  for (const [provider, create, extension] of [
    ["e2b", async () => E2BSandbox.create(), "getHost"],
    ["daytona", async () => new Daytona().create(), "getPreviewLink"],
    ["modal", async () => new ModalClient().sandboxes.create(), "tunnels"],
  ] as const) {
    const native = { [extension]: function () { return `${provider}:${this === native}`; } };
    const fake = installFakeCompute(new FakeSandbox(`${provider}-box`, provider, native));
    try {
      const sandbox = await create() as any;
      assert.equal(sandbox[extension](), `${provider}:true`);
    } finally {
      fake.cleanup();
    }
  }

  const native = { process: { advanced: () => "native-process-extension" } };
  const fake = installFakeCompute(new FakeSandbox("daytona-nested", "daytona", native));
  try {
    const sandbox = await new Daytona().create() as any;
    assert.equal(sandbox.process.advanced(), "native-process-extension");
  } finally {
    fake.cleanup();
  }
}

async function testDaytonaGetNotFound(): Promise<void> {
  const fake = installFakeCompute(new FakeSandbox("unused"), { getByIdResult: null });
  try {
    const daytona = new Daytona({ apiKey: "daytona-key" });
    await assert.rejects(() => daytona.get("missing"), /Sandbox missing not found/);
  } finally {
    fake.cleanup();
  }
}

async function testDaytonaImageBuilderIsNotDiscarded(): Promise<void> {
  const image = DaytonaImage.base("alpine:3.21").runCommands("echo expected-image");
  const fake = installFakeCompute(new FakeSandbox("native-image", "daytona"));
  try {
    await new Daytona().create({ image });
    assert.equal((fake.createCalls[0] as any).image, image);
  } finally {
    fake.cleanup();
  }
  let requests = 0;
  const client = new Daytona({ arker: {
    apiKey: "ark-test", source: "ubuntu", baseUrl: "https://arker.invalid/api",
    fetch: async () => { requests++; throw new Error("Must not boot a substituted image"); },
  } });
  await assert.rejects(() => client.create({ image }), /Daytona Image builders.*not supported on Arker/);
  assert.equal(requests, 0);
}

async function testDaytonaNativeExecutionPreservesResponseAndInputs(): Promise<void> {
  const response = { exitCode: 17, result: "native-output", artifacts: { stdout: "native-output", charts: [] } };
  const calls: unknown[] = [];
  const native = { process: {
    async executeCommand(...args: unknown[]) {
      assert.equal(this, native.process);
      calls.push(args);
      return response;
    },
  } };
  const fake = installFakeCompute(new FakeSandbox("native-exec", "daytona", native));
  try {
    const sandbox = await new Daytona().create();
    assert.equal(await sandbox.process.executeCommand("exit 17", "/tmp", { VALUE: "kept" }, 7), response);
    assert.deepEqual(calls, [["exit 17", "/tmp", { VALUE: "kept" }, 7]]);
  } finally { fake.cleanup(); }
}

async function testE2BSurface(): Promise<void> {
  const box = new FakeSandbox("e2b-box");
  const fake = installFakeCompute(box);
  try {
    const sandbox = await E2BSandbox.create("catalog-template", { timeoutMs: 123 });
    assertArkerFirst(fake.captured(), "e2b");
    assert.deepEqual(fake.createCalls[0], { templateId: "catalog-template", timeout: 123 });
    assert.equal(sandbox.sandboxId, "e2b-box");
    assert.equal(sandbox.getHost(8080), "p8080.example.invalid");
    assert.equal(sandbox.getHost(3000), "p8080-3000.example.invalid");

    await assert.rejects(() => sandbox.commands.run("printf e2b", { cwd: "/tmp", envs: { A: "b" }, timeoutMs: 200 }), (error: unknown) => {
      assert.ok(error instanceof CommandExitError);
      assert.equal(error.stdout, "stdout:printf e2b");
      assert.equal(error.stderr, "stderr:printf e2b");
      assert.equal(error.exitCode, 7);
      assert.equal(error.error, "exit status 7");
      return true;
    });
    assert.deepEqual(box.runCalls[0]!.options, { cwd: "/tmp", env: { A: "b" }, timeout: 200 });

    assert.equal(await sandbox.files.read("/tmp/a"), "read:/tmp/a");
    await sandbox.files.write("/tmp/a", "content");
    await sandbox.files.makeDir("/tmp/dir");
    assert.deepEqual(await sandbox.files.list("/tmp"), [{ name: "/tmp", type: "file", size: 1 }]);
    assert.equal(await sandbox.files.exists("/tmp/a"), true);
    await sandbox.files.remove("/tmp/a");
    assert.equal(await sandbox.kill(), true);
    assert.equal(await sandbox.kill(), false);
    assert.equal(box.destroyed, true);

    assert.throws(
      () => (sandbox.files as any).rename,
      /not supported by the Arker compatibility layer for e2b/,
    );
    await assert.rejects(
      () => E2BSandbox.create({ template: "bad" } as any),
      /unsupported option\(s\) 'template'/,
    );
    await E2BSandbox.create({ timeoutMs: 321 });
    assert.deepEqual(fake.createCalls.at(-1), { timeout: 321 });
    await assert.rejects(
      () => E2BSandbox.create("template", { envVars: { A: "B" } } as any),
      /unsupported option\(s\) 'envVars'/,
    );
  } finally {
    fake.cleanup();
  }
}

async function testE2BNativeErrorIdentity(): Promise<void> {
  const foreignExit = Object.assign(new Error("exit status 17"), {
    name: "CommandExitError", stdout: "output", stderr: "failure", exitCode: 17, error: "exit status 17",
  });
  const authentication = new Error("authentication refused");
  for (const failure of [foreignExit, authentication]) {
    const box = new FakeSandbox("native-e2b", "e2b", {
      commands: { run: async () => { throw failure; } },
    });
    installFakeCompute(box);
    try {
      const sandbox = await E2BSandbox.create();
      await assert.rejects(() => sandbox.commands.run("exit 17"), (error: unknown) => {
        if (failure === authentication) assert.equal(error, authentication);
        else {
          assert.ok(error instanceof CommandExitError);
          assert.equal(error.exitCode, 17);
          assert.equal(error.error, "exit status 17");
        }
        return true;
      });
    } finally { setCompatComputeFactoryForTests(); }
  }
}

async function testModalSurface(): Promise<void> {
  const box = new FakeSandbox("modal-box");
  const fake = installFakeCompute(box);
  const original = { exec: GuestProcesses.prototype.exec, get: GuestProcesses.prototype.get, readOutput: GuestProcesses.prototype.readOutput };
  const requests: unknown[] = [];
  const current = { pid: "7", status: "failed", exitCode: 7 } as any;
  GuestProcesses.prototype.exec = async (request) => { requests.push(request); return current; };
  GuestProcesses.prototype.get = async () => current;
  GuestProcesses.prototype.readOutput = async (_id, stream, offset) => {
    const bytes = new TextEncoder().encode(`${stream}:echo modal`);
    return { data: bytes.slice(offset), nextOffset: bytes.length, eof: true };
  };
  const client = new ModalClient({ tokenId: "modal-token-id", tokenSecret: "modal-token-secret" });
  try {
    const sandbox = await client.sandboxes.create();
    assertArkerFirst(fake.captured(), "modal");
    assert.equal(sandbox.sandboxId, "modal-box");
    assert.equal(typeof client.apps.fromName, "function");
    assert.equal(typeof client.images.fromRegistry, "function");
    for (const [service, method] of [
      ["secrets", "fromObject"], ["volumes", "fromName"],
      ["cloudBucketMounts", "create"], ["sandboxSnapshots", "fromId"],
      ["proxies", "fromName"], ["environments", "fromName"],
    ]) {
      const handle = (client as any)[service!];
      assert.equal(typeof handle[method!], "function", `${service}.${method}`);
      assert.equal((client as any)[service!], handle);
    }
    const secret = await client.secrets.fromObject({ INLINE_FIXTURE: "synthetic-only" });
    assert.ok(secret instanceof ModalSecret);
    assert.equal(secret.secretId, "");
    const mount = client.cloudBucketMounts.create("synthetic-bucket", { secret, readOnly: true });
    assert.ok(mount instanceof ModalBucketMount);
    assert.equal(mount.secret, secret);
    assert.equal(mount.readOnly, true);

    const app = { appId: "ap-native" };
    const image = { imageId: "im-native" };
    await client.sandboxes.create(app, image, { blockNetwork: true });
    assert.deepEqual(fake.createCalls.at(-1), { modalApp: app, modalImage: image, modalCreateParams: { blockNetwork: true } });
    await assert.rejects(() => client.sandboxes.create(app), /requires both app and image/);

    const proc = await sandbox.exec(["sh", "-c", "echo modal"], {
      workdir: "/workspace",
      env: { FOO: "bar" },
      timeoutMs: 456,
    });
    assert.equal(await proc.stdout.readText(), "stdout:echo modal");
    assert.equal(await proc.stderr.readText(), "stderr:echo modal");
    assert.equal(await proc.wait(), 7);
    assert.deepEqual(requests[0], {
      command: "sh -c 'echo modal'", argv: ["sh", "-c", "echo modal"], spawnFailureExitCode: 128, preserveDescendants: true, workingDir: "/workspace", env: { FOO: "bar" }, timeout: 456, stdin: true,
    });

    await sandbox.exec(["node", "script with space.js", "arg"]);
    assert.equal((requests[1] as any).command, "node 'script with space.js' arg");
    assert.deepEqual((requests[1] as any).argv, ["node", "script with space.js", "arg"]);

    await sandbox.terminate();
    assert.equal(box.destroyed, true);

    await assert.rejects(() => client.sandboxes.create("app", "image", {}, "extra"), /at most app, image, and options/);
    await assert.rejects(
      () => client.sandboxes.create(app, image, { unsupported: true }),
      /unsupported option\(s\) 'unsupported'/,
    );
    await assert.rejects(
      () => sandbox.exec("pwd", { pty: true } as any),
      /unsupported option\(s\) 'pty'/,
    );
    assert.throws(
      () => (client.sandboxes as any).list,
      /not supported by the Arker compatibility layer for modal/,
    );
  } finally {
    Object.assign(GuestProcesses.prototype, original);
    client.close();
    fake.cleanup();
  }
}

type FetchCall = {
  url: string;
  method: string;
  body?: string;
};

class FakeFetch {
  readonly calls: FetchCall[] = [];
  private readonly script: Array<{ predicate: (method: string, url: string) => boolean; response: Response }> = [];

  addJson(predicate: (method: string, url: string) => boolean, status: number, body: unknown): void {
    this.script.push({
      predicate,
      response: new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    });
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? init.body : undefined;
    this.calls.push({ url, method, body });

    const index = this.script.findIndex((entry) => entry.predicate(method, url));
    assert.notEqual(index, -1, `no scripted response for ${method} ${url}`);
    return this.script.splice(index, 1)[0]!.response;
  };
}

async function testInternalArkerProvider(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://arker.invalid/api/v1/fork",
    200,
    {
      vm_id: "vm_created",
      owner_org_id: "owner",
      created_at: "2026-01-01T00:00:00Z",
      public: false,
      state: "idle",
      hostname: "vm-created.aws-us-east-1.arker.app",
      sessions: [],
      tunnels: [],
    },
  );
  fetch.addJson(
    (method, url) => method === "GET" && url === "https://arker.invalid/api/v1/vms/missing",
    404,
    { code: "not_found", message: "missing" },
  );
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://arker.invalid/api/v1/vms/vm_created/runs",
    200,
    {
      state: "completed",
      stdout: "",
      stdout_encoding: "utf-8",
      stderr: "",
      stderr_encoding: "utf-8",
      exit_code: 0,
    },
  );

  const provider = createArkerComputeProvider({
    apiKey: "ark-key",
    baseUrl: "https://arker.invalid/api",
    fetch: fetch.fetch,
    retry: false,
  });

  const sandbox = await provider.sandbox.create({ templateId: "catalog-template", name: "created" });
  assert.equal(provider.name, "arker");
  assert.equal(sandbox.sandboxId, "vm_created");
  assert.equal((JSON.parse(fetch.calls[0]!.body!) as Record<string, unknown>).source_vm_name, "catalog-template");
  assert.equal((JSON.parse(fetch.calls[0]!.body!) as Record<string, unknown>).name, "created");
  assert.equal(await sandbox.getUrl({ port: 8080 }), "https://vm-created.aws-us-east-1.arker.app");
  assert.equal(await sandbox.getUrl({ port: 3000 }), "https://vm-created-3000.aws-us-east-1.arker.app");
  await assert.rejects(() => sandbox.getUrl({ port: 3000, protocol: "http" }), /use HTTPS/);
  await assert.rejects(() => sandbox.getUrl({ port: 0 }), /port must be an integer/);

  const missing = await provider.sandbox.getById("missing");
  assert.equal(missing, null);

  await sandbox.runCommand("sleep 1", { timeout: 1_500 });
  const runBody = JSON.parse(fetch.calls[2]!.body!) as Record<string, unknown>;
  assert.equal(runBody.timeout, 2);

  await assert.rejects(
    () => provider.sandbox.create({ templateId: "source", envs: { A: "b" } }),
    /does not support option\(s\): envs/,
  );
}

async function testInternalArkerProviderImageAndResources(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://arker.invalid/api/v1/fork",
    200,
    { vm_id: "vm_image", owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] },
  );
  const provider = createArkerComputeProvider({ apiKey: "ark-key", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false });
  await assert.rejects(
    () => provider.sandbox.create({ image: DaytonaImage.base("alpine:3.21") } as any),
    /image must be a published image reference/,
  );
  await provider.sandbox.create({ image: "ubuntu:24.04", cpu: 4, memory: 8192, diskMiB: 20480, name: "image-box" });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), {
    image: "ubuntu:24.04",
    name: "image-box",
    resources: { vcpu: 4, memory_mib: 8192, disk_mib: 20480 },
  });
}

async function testInternalArkerDirectoryNames(): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "arker-directory-parity-"));
  try {
    const names = ["line\nbreak", "tab\tname", "-leading", ".hidden", "quote'file", "back\\slash"];
    for (const name of names) writeFileSync(join(directory, name), "hello");
    mkdirSync(join(directory, "directory\nname"));
    symlinkSync("missing", join(directory, "dangling"));
    const fetch = new FakeFetch();
    fetch.addJson((method, url) => method === "POST" && url.endsWith("/v1/fork"), 200,
      { vm_id: "vm_directory", owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] });
    const localFetch: typeof globalThis.fetch = async (input, init) => {
      if (String(input).endsWith("/runs")) {
        const request = JSON.parse(String(init?.body));
        return Response.json({ state: "completed", stdout: execFileSync("/bin/sh", ["-c", request.command], { encoding: "utf8", cwd: directory }), stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8", exit_code: 0 });
      }
      return fetch.fetch(input, init);
    };
    const provider = createArkerComputeProvider({ apiKey: "test", source: "ubuntu", baseUrl: "https://arker.invalid/api", fetch: localFetch, retry: false });
    const sandbox = await provider.sandbox.create();
    const entries = await sandbox.filesystem.readdir(directory);
    assert.deepEqual(entries.map(entry => entry.name).sort(), [...names, "directory\nname", "dangling"].sort());
    for (const entry of entries.filter(entry => names.includes(entry.name))) assert.equal(entry.size, 5);
    assert.equal(entries.find(entry => entry.name === "directory\nname")?.type, "directory");
    mkdirSync(join(directory, "--list"));
    mkdirSync(join(directory, "--remove"));
    const failures: string[] = [];
    for (const [name, action] of [
      ["mkdir", () => sandbox.filesystem.mkdir("--mkdir")],
      ["readdir", () => sandbox.filesystem.readdir("--list")],
      ["remove", () => sandbox.filesystem.remove("--remove")],
    ] as const) {
      try { await action(); } catch { failures.push(name); }
    }
    assert.deepEqual(failures, [], "relative leading-dash paths must not become shell options");
    assert.ok(existsSync(join(directory, "--mkdir")));
    assert.ok(!existsSync(join(directory, "--remove")));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

async function testInternalArkerProviderSourceProvenance(): Promise<void> {
  const fetch = new FakeFetch();
  for (const id of ["vm_from_template", "vm_from_snapshot"]) {
    fetch.addJson(
      (method, url) => method === "POST" && url === "https://arker.invalid/api/v1/fork",
      200,
      { vm_id: id, owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] },
    );
  }
  const provider = createArkerComputeProvider({ apiKey: "ark-key", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false });
  await provider.sandbox.create({ templateId: "vm-template-name" });
  await provider.sandbox.create({ snapshotId: "snapshot-without-vm-prefix" });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), { source_vm_name: "vm-template-name" });
  assert.deepEqual(JSON.parse(fetch.calls[1]!.body!), { source_vm_id: "snapshot-without-vm-prefix" });
}

async function testSharedForkPolicySecretsAndRegistry(): Promise<void> {
  const fetch = new FakeFetch();
  for (const id of ["vm_child", "vm_image"]) {
    fetch.addJson((method, url) => method === "POST" && url.endsWith("/v1/fork"), 200,
      { vm_id: id, owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] });
  }
  const policies = { secrets: { TEST_TOKEN: "synthetic-policy-secret" }, rules: { inherited: { type: "outbound", action: "allow" } } } as any;
  const registryAuth = { username: "synthetic", password: "synthetic-registry-password" };
  const provider = createArkerComputeProvider({ apiKey: "test", sourceVmId: "vm_image_parent", policies, registryAuth, baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false });
  await provider.sandbox.create({ blockNetwork: true });
  await provider.sandbox.create({ image: "private.example/image@sha256:abc" });
  const [child, image] = fetch.calls.map(call => JSON.parse(call.body!));
  assert.equal(child.source_vm_id, "vm_image_parent");
  assert.equal(child.registry_auth, undefined);
  assert.deepEqual(child.policies.secrets, policies.secrets);
  assert.deepEqual(child.policies.rules.inherited, policies.rules.inherited);
  const guards = Object.entries(child.policies.rules).filter(([, rule]: any) => rule.type === "outbound_guard");
  assert.equal(guards.length, 1);
  assert.deepEqual(guards[0]![1], { type: "outbound_guard", allow: [] });
  assert.deepEqual(image.registry_auth, registryAuth);
  assert.deepEqual(image.policies, policies);
  assert.equal(image.source_vm_id, undefined);
  assert.ok(policies.rules.inherited, "network translation must not mutate caller policy config");
}

async function testDaytonaResourceUnits(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson((method, url) => method === "POST" && url.endsWith("/v1/fork"), 200,
    { vm_id: "vm_resources", owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] });
  fetch.addJson((method, url) => method === "POST" && url.endsWith("/runs"), 200,
    { state: "completed", stdout: "", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8", exit_code: 0 });
  const client = new Daytona({ arker: { apiKey: "ark-key", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false } });
  await client.create({ image: "ubuntu:24.04", resources: { cpu: 4, memory: 8, disk: 20 } });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!).resources, { vcpu: 4, memory_mib: 8192, disk_mib: 20480 });
}

async function testInternalArkerProviderPolicyReplacement(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson(
    (method, url) => method === "POST" && url === "https://arker.invalid/api/v1/fork",
    200,
    { vm_id: "vm_policy", owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] },
  );
  const provider = createArkerComputeProvider({
    apiKey: "ark-key",
    baseUrl: "https://arker.invalid/api",
    source: "catalog-template",
    fetch: fetch.fetch,
    retry: false,
  });
  await provider.sandbox.create({
    outboundDomainAllowlist: ["api.example.com"],
    outboundCidrAllowlist: ["203.0.113.0/24"],
  } as any);
  const body = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(Object.keys(body.policies.rules).length, 1);
  assert.deepEqual(Object.values(body.policies.rules)[0], {
    type: "outbound_guard",
    allow: [
      { hosts: ["api.example.com"], ports: [443] },
      { ips: ["203.0.113.0/24"] },
    ],
  });
}

async function testOrderedPolicyControlsSurviveNativeNetworkRestrictions(): Promise<void> {
  const fetch = new FakeFetch();
  for (const id of ["vm_first", "vm_second"]) {
    fetch.addJson((method, url) => method === "POST" && url.endsWith("/v1/fork"), 200,
      { vm_id: id, owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z", public: false, state: "idle", sessions: [], tunnels: [] });
  }
  const policies = {
    secrets: { TOKEN: "synthetic-only" },
    policies: [{ type: "inbound", action: "deny" }],
  } as any;
  const original = structuredClone(policies);
  const provider = createArkerComputeProvider({ apiKey: "test", source: "parent", policies,
    baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false });
  await provider.sandbox.create({ blockNetwork: true });
  await provider.sandbox.create({ blockNetwork: true });
  const identifiers: string[] = [];
  for (const call of fetch.calls) {
    const written = JSON.parse(call.body!).policies;
    assert.deepEqual(written.policies, policies.policies);
    assert.deepEqual(written.secrets, policies.secrets);
    assert.equal(Object.keys(written.rules).length, 1);
    const [id, guard] = Object.entries(written.rules)[0]!;
    assert.match(id, /^[a-z0-9][a-z0-9._-]{0,63}$/);
    assert.deepEqual(guard, { type: "outbound_guard", allow: [] });
    identifiers.push(id);
  }
  assert.notEqual(identifiers[0], identifiers[1]);
  assert.deepEqual(policies, original);
}

async function testBlaxelCreateIfNotExistsAndTtl(): Promise<void> {
  const created = new FakeSandbox("new-box");
  const fake = installFakeCompute(created, { getByIdResult: null });
  try {
    await BlaxelSandbox.createIfNotExists({ name: "named-box" }, { timeout: 30 });
    await BlaxelSandbox.create({
      name: "new-box",
      ttl: "5m",
      region: "us-pdx-1",
      ports: [{ target: 3000, protocol: "HTTP" }],
      labels: { env: "test" },
      network: { firewall: { rulesets: ["proxy"] }, allowedDomains: ["example.com"], proxy: { routing: [] } },
    });
    assert.deepEqual(fake.createCalls[0], {
      name: "named-box", image: undefined, memory: undefined, timeout: undefined, envs: undefined, runtime: undefined,
      createIfNotExist: true,
      creationTimeout: 30,
    });
    assert.deepEqual(fake.createCalls[1], {
      name: "new-box", image: undefined, memory: undefined, timeout: 300_000, envs: undefined, runtime: undefined,
      region: "us-pdx-1",
      ports: [{ target: 3000, protocol: "HTTP" }],
      labels: { env: "test" },
      network: { firewall: { rulesets: ["proxy"] }, allowedDomains: ["example.com"], proxy: { routing: [] } },
      createIfNotExist: false,
    });
    await assert.rejects(() => BlaxelSandbox.create({ ttl: "eventually" }), /Invalid Blaxel ttl/);
    const arker = createArkerComputeProvider({ apiKey: "test", baseUrl: "https://example.invalid" });
    await assert.rejects(() => arker.sandbox.create({ network: { firewall: { rulesets: ["proxy"] } } } as any), /network/);
    await assert.rejects(() => arker.sandbox.create({ createIfNotExist: true } as any), /createIfNotExist/);
  } finally {
    fake.cleanup();
  }
}

async function testBlaxelInitializationFailureDeletesCreatedVm(): Promise<void> {
  const created = new FakeSandbox("failed-initialization");
  created.filesystem.mkdir = async () => { throw new Error("initialization failed"); };
  const fake = installFakeCompute(created);
  try {
    await assert.rejects(BlaxelSandbox.create(), /initialization failed/);
    assert.equal(created.destroyed, true);
    assert.equal(fake.createCalls.length, 1);
  } finally { fake.cleanup(); }
}

async function testBlaxelBinaryFiles(): Promise<void> {
  const files = new Map<string, Uint8Array>();
  const vm = {
    sync: async (path: string, content?: Uint8Array) => {
      if (content === undefined) return files.get(path)!;
      files.set(path, new Uint8Array(content));
    },
  };
  const box = new FakeSandbox("binary-vm", "arker", vm);
  const fake = installFakeCompute(box);
  try {
    const sandbox = await BlaxelSandbox.create();
    const payload = Uint8Array.from({ length: 256 }, (_, i) => i);
    assert.deepEqual(await sandbox.fs.mkdir("relative"), {
      message: "Directory created successfully", path: "/blaxel/relative",
    });
    const writesBefore = box.createOptions.length;
    await assert.rejects(() => sandbox.fs.mkdir("private", "0700"), /permissions.*not supported/);
    assert.equal(box.createOptions.length, writesBefore);
    await sandbox.fs.writeBinary("binary", payload);
    assert.deepEqual(new Uint8Array(await (await sandbox.fs.readBinary("binary")).arrayBuffer()), payload);
    await sandbox.fs.writeBinary("binary", new Blob([payload]));
    assert.deepEqual(files.get("/blaxel/binary"), payload);
    await sandbox.fs.writeBinary("empty", new Uint8Array());
    assert.equal((await sandbox.fs.readBinary("empty")).size, 0);
  } finally { fake.cleanup(); }
}

async function testBlaxelNonrecursiveRemoval(): Promise<void> {
  const box = new FakeSandbox();
  const fake = installFakeCompute(box);
  box.runCommand = async (command, options) => {
    box.runCalls.push({ command, options });
    return { stdout: "file", stderr: "", exitCode: 0, durationMs: 0 };
  };
  try {
    const sandbox = await BlaxelSandbox.create();
    await sandbox.fs.rm("/blaxel/directory");
    assert.ok(box.runCalls.at(-1)?.command.includes("rmdir --"));
    await sandbox.fs.rm("/blaxel/directory", true);
    assert.ok(box.runCalls.at(-1)?.command.includes("rm -r --"));
  } finally { fake.cleanup(); }
}

async function testBlaxelDirectoryPaths(): Promise<void> {
  const box = new FakeSandbox();
  box.filesystem.readdir = async (path) => {
    assert.equal(path, "/blaxel/nested");
    return [
      { name: "line\nbreak", type: "file", size: 7 },
      { name: "child", type: "directory" },
    ];
  };
  const fake = installFakeCompute(box);
  try {
    const sandbox = await BlaxelSandbox.create();
    const listing = await sandbox.fs.ls("nested/");
    assert.equal(listing.name, "nested");
    assert.equal(listing.path, "/blaxel/nested");
    assert.equal(listing.files[0]?.path, "/blaxel/nested/line\nbreak");
    assert.equal(listing.files[0]?.size, 7);
    assert.equal(listing.subdirectories[0]?.path, "/blaxel/nested/child");
  } finally { fake.cleanup(); }
}

async function testBackgroundProcessOwnsAnArkerRun(): Promise<void> {
  const calls: any[] = [];
  const vm = {
    createSession: async () => ({ session_id: "process-session" }),
    run: async (command: string, options: unknown) => {
      calls.push({ command, options });
      return { type: "background", runId: "supervisor-run" };
    },
    getRun: async () => ({ state: "running", stdout: JSON.stringify({ result: { name: "background", status: "running" } }) + "\n", stderr: "" }),
  };
  const manager = new GuestProcesses(new FakeSandbox("process-vm", "arker", vm));
  const started = await manager.exec({ command: "sleep 30" });
  assert.equal(started.status, "running");
  assert.ok(calls[0].command.includes(" supervise "));
  assert.deepEqual(calls[0].options, { session_id: "process-session", time_to_background: 0 });
}

async function testProcessControlHasAnIsolatedSerializedSession(): Promise<void> {
  let sessions = 0;
  let active = 0;
  const ids: string[] = [];
  const vm = {
    createSession: async () => ({ session_id: `control-${++sessions}` }),
    run: async (_command: string, options: { session_id: string }) => {
      ids.push(options.session_id);
      assert.equal(++active, 1, "control requests must not collide within a shared session");
      await Promise.resolve();
      active--;
      return { type: "completed", stdout: JSON.stringify({ result: { name: "controlled" } }), stderr: "", exitCode: 0 };
    },
  };
  const manager = new GuestProcesses(new FakeSandbox("control-vm", "arker", vm));
  const results = await Promise.all([manager.get("controlled"), manager.get("controlled")]);
  assert.deepEqual(results.map((result) => result.name), ["controlled", "controlled"]);
  assert.equal(sessions, 1);
  assert.deepEqual(ids, ["control-1", "control-1"]);
}

async function testInternalArkerProviderRequiresSource(): Promise<void> {
  const previousSource = process.env.ARKER_SOURCE;
  const previousSourceVm = process.env.ARKER_SOURCE_VM;
  delete process.env.ARKER_SOURCE;
  delete process.env.ARKER_SOURCE_VM;
  const provider = createArkerComputeProvider({
    apiKey: "ark-key",
    baseUrl: "https://arker.invalid/api",
    retry: false,
  });

  try {
    await assert.rejects(() => provider.sandbox.create(), /Arker source is required/);
  } finally {
    if (previousSource === undefined) delete process.env.ARKER_SOURCE;
    else process.env.ARKER_SOURCE = previousSource;
    if (previousSourceVm === undefined) delete process.env.ARKER_SOURCE_VM;
    else process.env.ARKER_SOURCE_VM = previousSourceVm;
  }
}

await testDaytonaSurface();
await testNativeProviderExtensionsAreDelegated();
await testDaytonaGetNotFound();
await testDaytonaImageBuilderIsNotDiscarded();
await testDaytonaNativeExecutionPreservesResponseAndInputs();
await testE2BSurface();
await testE2BNativeErrorIdentity();
await testModalSurface();
await testInternalArkerProvider();
await testInternalArkerProviderImageAndResources();
await testInternalArkerDirectoryNames();
await testInternalArkerProviderSourceProvenance();
await testSharedForkPolicySecretsAndRegistry();
await testDaytonaResourceUnits();
await testInternalArkerProviderPolicyReplacement();
await testOrderedPolicyControlsSurviveNativeNetworkRestrictions();
await testInternalArkerProviderRequiresSource();
await testBlaxelCreateIfNotExistsAndTtl();
await testBlaxelInitializationFailureDeletesCreatedVm();
await testBlaxelBinaryFiles();
await testBlaxelNonrecursiveRemoval();
await testBlaxelDirectoryPaths();
await testBackgroundProcessOwnsAnArkerRun();
await testProcessControlHasAnIsolatedSerializedSession();

async function testSharedRouterPreservesNativeCreateError(): Promise<void> {
  const previous = process.env.ARKER_API_KEY;
  delete process.env.ARKER_API_KEY;
  const conflict = { code: "SANDBOX_ALREADY_EXISTS", message: "exists" };
  try {
    const sdk = createCompatSdk({ fallbackProvider: {
      name: "blaxel",
      instances: {
        create: async () => { throw conflict; },
        getById: async () => null,
        destroy: async () => undefined,
      },
    } });
    await assert.rejects(sdk.sandbox.create(), (error) => error === conflict);
  } finally {
    if (previous === undefined) delete process.env.ARKER_API_KEY;
    else process.env.ARKER_API_KEY = previous;
  }
}
await testSharedRouterPreservesNativeCreateError();

console.log("PASS compat");
