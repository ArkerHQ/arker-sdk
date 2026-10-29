import { e2bForkMetadata } from "../src/compat/e2b-metadata.js";
import assert from "node:assert/strict";
import { FakeFetch as ScriptedFetch } from "./helpers/scripted-fetch.js";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync, symlinkSync, existsSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import type { CommandResult, FileEntry, RunCommandOptions, SandboxInfo, SandboxInterface } from "computesdk";

import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { CommandExitError, Sandbox as NativeE2BSandbox } from "e2b";
import { Image as DaytonaImage } from "@daytona/sdk";
import { Secret as ModalSecret, CloudBucketMount as ModalBucketMount } from "modal";
import { ArkerError } from "../src/index.js";
import { SandboxInstance as BlaxelSandbox } from "../src/blaxel.js";
import { ModalClient } from "../src/modal.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";
import { createCompatSdk, setCompatComputeFactoryForTests } from "../src/compat/runtime.js";
import { GuestProcesses } from "../src/compat/guest-process.js";

// Source-selection tests configure their own IDs rather than inheriting a live baseline.
delete process.env.ARKER_SOURCE_VM_ID;

type CapturedComputeConfig = {
  providers: Array<{ name?: string }>;
};

class FakeSandbox implements SandboxInterface {
  readonly instanceId: string;
  readonly sandboxId: string;
  readonly provider: string;
  readonly runCalls: Array<{ command: string; options?: RunCommandOptions }> = [];
  runResult?: CommandResult;
  readonly createOptions: unknown[] = [];
  destroyed = false;

  constructor(sandboxId = "fake-arker-vm", provider = "arker", private readonly native?: object) {
    this.sandboxId = sandboxId;
    this.instanceId = sandboxId;
    this.provider = provider;
  }

  readonly policies = { rules: {} };
  async update() {}

  getInstance(): object { return this.native ?? this; }

  async sync(_path: string, content?: Uint8Array): Promise<Uint8Array | void> {
    return content === undefined ? new Uint8Array() : undefined;
  }

  async runCommand(command: string, options?: RunCommandOptions): Promise<CommandResult> {
    if (command.startsWith("command -v setpriv >/dev/null ")) {
      return { stdout: "", stderr: "", exitCode: 0, durationMs: 0 };
    }
    this.runCalls.push({ command, options });
    if (this.runResult) return this.runResult;
    if (command.includes(".computesdk-e2b-write-") || command.startsWith("rm -f -- ")) {
      return { stdout: "", stderr: "", exitCode: 0, durationMs: 0 };
    }
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

async function withFakeCompute(
  box: FakeSandbox,
  run: (fake: ReturnType<typeof installFakeCompute>) => Promise<void>,
  options: Parameters<typeof installFakeCompute>[1] = {},
): Promise<void> {
  const fake = installFakeCompute(box, options);
  try { await run(fake); } finally { fake.cleanup(); }
}

function assertArkerFirst(config: CapturedComputeConfig, fallbackName: string): void {
  void fallbackName;
  assert.deepEqual(config.providers.map((provider) => provider.name), ["arker-compat-router"]);
}

async function testDaytonaSurface(): Promise<void> {
  const box = new FakeSandbox("daytona-box");
  await withFakeCompute(box, async (fake) => {
    const daytona = new Daytona({ apiKey: "daytona-key", arker: { apiKey: "ark-key", baseUrl: "https://arker.invalid/api" } });
    const sandbox = await daytona.create();
    assertArkerFirst(fake.captured(), "daytona");
    assert.equal(sandbox.id, "daytona-box");

    const result = await sandbox.process.exec("echo hi", { cwd: "/workspace", env: { A: "b" }, timeout: 100 });
    assert.equal(result.exitCode, 7);
    assert.equal(result.result, `stdout:${box.runCalls[0]!.command}`);
    assert.equal(result.stdout, `stdout:${box.runCalls[0]!.command}`);
    assert.equal(result.stderr, `stderr:${box.runCalls[0]!.command}`);
    assert.match(box.runCalls[0]!.command, /setpriv --reuid=daytona/);
    assert.match(box.runCalls[0]!.command, /"command":"echo hi","cwd":"\/workspace","env":\{"A":"b"\}/);
    assert.deepEqual(box.runCalls[0]!.options, { timeout: 100_000 });
    const executeCommandResult = await sandbox.process.executeCommand("echo compat", "/official", { OFFICIAL: "yes" }, 12);
    assert.equal(executeCommandResult.result, `stdout:${box.runCalls[1]!.command}`);
    assert.match(box.runCalls[1]!.command, /"command":"echo compat","cwd":"\/official","env":\{"OFFICIAL":"yes"\}/);
    assert.deepEqual(box.runCalls[1]!.options, { timeout: 12_000 });

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
  });
}

async function testNativeProviderExtensionsAreDelegated(): Promise<void> {
  for (const [provider, create, extension] of [
    ["e2b", async () => E2BSandbox.create(), "getHost"],
    ["daytona", async () => new Daytona({ apiKey: "daytona-test" }).create(), "getPreviewLink"],
    ["modal", async () => new ModalClient().sandboxes.create(), "tunnels"],
  ] as const) {
    const native = { [extension]: function () { return `${provider}:${this === native}`; } };
    await withFakeCompute(new FakeSandbox(`${provider}-box`, provider, native), async () => {
      const sandbox = await create() as any;
      assert.equal(sandbox[extension](), `${provider}:true`);
    });
  }

  const native = { process: { advanced: () => "native-process-extension" } };
  await withFakeCompute(new FakeSandbox("daytona-nested", "daytona", native), async () => {
    const sandbox = await new Daytona({ apiKey: "daytona-test" }).create() as any;
    assert.equal(sandbox.process.advanced(), "native-process-extension");
  });
}

async function testDaytonaGetNotFound(): Promise<void> {
  await withFakeCompute(new FakeSandbox("unused"), async (fake) => {
    const daytona = new Daytona({ apiKey: "daytona-key" });
    await assert.rejects(() => daytona.get("missing"), /Sandbox missing not found/);
  }, { getByIdResult: null });
}

async function testDaytonaImageBuilderIsNotDiscarded(): Promise<void> {
  const image = DaytonaImage.base("alpine:3.21").runCommands("echo expected-image");
  await withFakeCompute(new FakeSandbox("native-image", "daytona"), async (fake) => {
    await new Daytona({ apiKey: "daytona-test" }).create({ image });
    assert.equal((fake.createCalls[0] as any).image, image);
  });
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
  await withFakeCompute(new FakeSandbox("native-exec", "daytona", native), async () => {
    const sandbox = await new Daytona({ apiKey: "daytona-test" }).create();
    assert.equal(await sandbox.process.executeCommand("exit 17", "/tmp", { VALUE: "kept" }, 7), response);
    assert.deepEqual(calls, [["exit 17", "/tmp", { VALUE: "kept" }, 7]]);
  });
}

async function testE2BSurface(): Promise<void> {
  const box = new FakeSandbox("e2b-box");
  const factory = E2BSandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  const native = new NativeE2BSandbox({ sandboxId: "local", sandboxDomain: "example.invalid", envdVersion: "0.10.2" });
  const components = { commands: native.commands, files: native.files, pty: native.pty, git: native.git, close() {} };
  factory.dataPlane = async () => components;
  try { await withFakeCompute(box, async (fake) => {
    const sandbox = await E2BSandbox.create("catalog-template", { timeoutMs: 123 });
    assertArkerFirst(fake.captured(), "e2b");
    assert.deepEqual(fake.createCalls[0], { templateId: "catalog-template", timeoutMs: 123 });
    assert.equal(sandbox.sandboxId, "e2b-box");
    assert.equal(sandbox.getHost(8080), "p8080.example.invalid");
    assert.equal(sandbox.getHost(3000), "p8080-3000.example.invalid");
    for (const name of ["commands", "files", "pty", "git"] as const) assert.equal(sandbox[name], components[name]);
    await E2BSandbox.create({ timeoutMs: 321 });
    assert.deepEqual(fake.createCalls.at(-1), { timeoutMs: 321 });

  }); } finally { factory.dataPlane = original; }
}

async function testE2BNativeErrorIdentity(): Promise<void> {
  const foreignExit = new CommandExitError({ stdout: "output", stderr: "failure", exitCode: 17, error: "exit status 17" });
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

function localE2BCommand(box: FakeSandbox): FakeSandbox["runCommand"] {
  return async (command, options) => {
    if (command.startsWith("command -v setpriv >/dev/null ")) {
      return { stdout: "", stderr: "", exitCode: 0, durationMs: 0 };
    }
    box.runCalls.push({ command, options });
    // This local guest maps both guest identities to the test runner's account.
    for (const user of ["user", "root"]) {
      command = command.replace(`setpriv --reuid=${user} --regid=${user} --init-groups -- `, "");
    }
    command = command.replaceAll("pwd.getpwnam(user)", "pwd.getpwuid(os.getuid())");
    try {
      return {
        stdout: execFileSync("/bin/bash", ["-lc", command], { encoding: "utf8" }),
        stderr: "",
        exitCode: 0,
        durationMs: 0,
      };
    } catch (error) {
      const failure = error as { stdout?: string; stderr?: string; status?: number };
      return {
        stdout: failure.stdout ?? "",
        stderr: failure.stderr ?? "",
        exitCode: failure.status ?? 1,
        durationMs: 0,
      };
    }
  };
}

async function testE2BArkerGitRunsOfficialWorkflowAgainstLocalRepository(): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), "arker-e2b-git-"));
  const remote = join(root, "remote.git");
  const checkout = join(root, "checkout");
  const box = new FakeSandbox("git-vm");
  box.runCommand = localE2BCommand(box);
  const fake = installFakeCompute(box);
  const factory = E2BSandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  const native = new NativeE2BSandbox({ sandboxId: "local", sandboxDomain: "example.invalid", envdVersion: "0.10.2" });
  const Git = native.git.constructor as new (commands: unknown) => typeof native.git;
  factory.dataPlane = async () => ({ commands: {}, files: {}, pty: {}, git: new Git({ run: async (command: string) => {
    const result = await box.runCommand(command);
    if (result.exitCode) throw new CommandExitError(result);
    return result;
  } }), close() {} });
  try {
    const sandbox = await E2BSandbox.create();
    await sandbox.git.init(remote, { bare: true, initialBranch: "main" });
    await sandbox.git.clone(remote, { path: checkout });
    await sandbox.git.configureUser("Arker Test", "test@arker.invalid", { scope: "local", path: checkout });
    writeFileSync(join(checkout, "hello.txt"), "hello\n");
    await sandbox.git.add(checkout, { files: ["hello.txt"] });
    await sandbox.git.commit(checkout, "initial");
    await sandbox.git.createBranch(checkout, "feature");

    assert.equal((await sandbox.git.status(checkout)).isClean, true);
    const branches = await sandbox.git.branches(checkout);
    assert.equal(branches.currentBranch, "feature");
    assert.deepEqual(new Set(branches.branches), new Set(["feature", "main"]));
  } finally {
    factory.dataPlane = original;
    fake.cleanup();
    rmSync(root, { recursive: true, force: true });
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
      timeoutMs: 1000,
    });
    assert.equal(await proc.stdout.readText(), "stdout:echo modal");
    assert.equal(await proc.stderr.readText(), "stderr:echo modal");
    assert.equal(await proc.wait(), 7);
    assert.deepEqual(requests[0], {
      command: "sh -c 'echo modal'", argv: ["sh", "-c", "echo modal"], spawnFailureExitCode: 128, preserveDescendants: true, workingDir: "/workspace", env: { FOO: "bar" }, timeout: 1000, stdin: true,
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
      /Sandbox.exec PTY is not supported.*pty must be false or omitted/,
    );
    assert.equal(typeof client.sandboxes.list, "function");
  } finally {
    Object.assign(GuestProcesses.prototype, original);
    client.close();
    fake.cleanup();
  }
}

function vmPayload(vmId: string) {
  return { vm_id: vmId, owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z",
    public: false, state: "idle", sessions: [], tunnels: [] };
}

class FakeFetch extends ScriptedFetch {
  addFork(vmId: string): void {
    this.addJson((method, url) => method === "POST" && url === "https://arker.invalid/api/v1/fork",
      200, vmPayload(vmId));
  }
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
  fetch.addFork("vm_image");
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

async function testInternalArkerProviderMapsLifetimeToServerSchedule(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addFork("vm_ttl");
  const configuredPolicies = {
    secrets: { TOKEN: "synthetic" },
    policies: [{ type: "outbound", priority: 1, action: "deny" }],
    rules: { custom: { type: "outbound", priority: 7, action: "allow" } },
  } as any;
  const provider = createArkerComputeProvider({
    apiKey: "ark-key", source: "ubuntu", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false,
    policies: configuredPolicies,
  });
  await provider.sandbox.create({ timeout: 17_000, blockNetwork: true });
  const rules = (JSON.parse(fetch.calls[0]!.body!) as any).policies.rules;
  assert.deepEqual(rules.custom, { type: "outbound", priority: 7, action: "allow" });
  assert.deepEqual(Object.values(rules).filter((rule: any) => rule.type === "schedule"), [
    { type: "schedule", after_created: 17, action: "delete" },
  ]);
  assert.equal(Object.values(rules).filter((rule: any) => rule.type === "outbound_guard").length, 1);
  const sentPolicies = (JSON.parse(fetch.calls[0]!.body!) as any).policies;
  assert.deepEqual(sentPolicies.secrets, { TOKEN: "synthetic" });
  assert.deepEqual(sentPolicies.policies, [{ type: "outbound", priority: 1, action: "deny" }]);
  assert.deepEqual(configuredPolicies, {
    secrets: { TOKEN: "synthetic" },
    policies: [{ type: "outbound", priority: 1, action: "deny" }],
    rules: { custom: { type: "outbound", priority: 7, action: "allow" } },
  });
  for (const timeout of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, 86_400_001]) {
    await assert.rejects(() => provider.sandbox.create({ timeout }), /timeout must be a positive finite number/);
  }
  assert.equal(fetch.calls.length, 1, "invalid lifetimes must not issue a fork request");
  const conflicting = createArkerComputeProvider({
    apiKey: "ark-key", source: "ubuntu", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false,
    policies: { rules: { "computesdk-e2b-lifetime": null } } as any,
  });
  await assert.rejects(() => conflicting.sandbox.create({ timeout: 1000 }), /policy id.*reserved/);
  assert.equal(fetch.calls.length, 1, "reserved policy collisions must not issue a fork request");
}

async function assertCreateRejectedBeforeRequest(options: Record<string, unknown>, error: RegExp): Promise<void> {
  let requests = 0;
  const provider = createArkerComputeProvider({
    apiKey: "test", baseUrl: "https://arker.invalid/api", retry: false,
    fetch: async () => { requests++; return Response.json(vmPayload("unexpected-vm")); },
  });
  await assert.rejects(() => provider.sandbox.create({ templateId: "source", ...options }), error);
  assert.equal(requests, 0);
}

async function testInternalArkerProviderRejectsUntranslatedPolicyControls(): Promise<void> {
  for (const [key, value] of Object.entries({
    networkPolicy: { allowInternetAccess: false }, networkPolicies: [],
    internetAccess: false, autoStopInterval: 0,
  })) {
    await assertCreateRejectedBeforeRequest({ [key]: value }, new RegExp(`does not support option\\(s\\): ${key}`));
  }
}

async function testModalEmptyAllowlistIsNotUnrestricted(): Promise<void> {
  for (const key of ["outboundCidrAllowlist", "outboundDomainAllowlist"]) {
    const fetch = new FakeFetch();
    fetch.addJson(() => true, 200, {
      vm_id: "restricted-vm", owner_org_id: "owner", created_at: "2026-01-01T00:00:00Z",
      public: false, state: "idle", sessions: [], tunnels: [],
    });
    const provider = createArkerComputeProvider({
      apiKey: "test", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false,
    });
    await provider.sandbox.create({ templateId: "source", [key]: [] });
    const request = JSON.parse(fetch.calls[0]!.body!);
    assert.ok(request.policies, key);
    assert.deepEqual(Object.values(request.policies.rules), [{ type: "outbound_guard", allow: [] }]);
  }
}

async function testInvalidNetworkAllowlistsNeverMeanUnrestricted(): Promise<void> {
  for (const key of ["domainAllowList", "networkAllowList", "outboundDomainAllowlist", "outboundCidrAllowlist"]) {
    for (const value of [{}, false, 7, [7]]) {
      await assertCreateRejectedBeforeRequest({ [key]: value }, /allowlist/);
    }
  }
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
      vmPayload("vm_directory"));
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
    fetch.addFork(id);
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
      vmPayload(id));
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

async function testSharedForkParentIdEnvironment(): Promise<void> {
  const previous = process.env.ARKER_SOURCE_VM_ID;
  process.env.ARKER_SOURCE_VM_ID = "environment-parent-id";
  try {
    for (const configuredId of [undefined, "configured-parent-id"]) {
      const fetch = new FakeFetch();
      fetch.addJson((method, url) => method === "POST" && url.endsWith("/v1/fork"), 200,
        vmPayload("vm_child"));
      const provider = createArkerComputeProvider({ apiKey: "test", sourceVmId: configuredId,
        baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false });
      await provider.sandbox.create();
      const request = JSON.parse(fetch.calls[0]!.body!);
      assert.equal(request.source_vm_id, configuredId ?? "environment-parent-id");
      assert.equal(request.source_vm_name, undefined);
    }
  } finally {
    if (previous === undefined) delete process.env.ARKER_SOURCE_VM_ID;
    else process.env.ARKER_SOURCE_VM_ID = previous;
  }
}

async function testDaytonaResourceUnits(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addJson((method, url) => method === "POST" && url.endsWith("/v1/fork"), 200,
    vmPayload("vm_resources"));
  fetch.addJson((method, url) => method === "POST" && url.endsWith("/runs"), 200,
    { state: "completed", stdout: "", stdout_encoding: "utf-8", stderr: "", stderr_encoding: "utf-8", exit_code: 0 });
  const client = new Daytona({ arker: { apiKey: "ark-key", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false } });
  await client.create({ image: "ubuntu:24.04", resources: { cpu: 4, memory: 8, disk: 20 } });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!).resources, { vcpu: 4, memory_mib: 8192, disk_mib: 20480 });
}

async function testInternalArkerProviderPolicyReplacement(): Promise<void> {
  const fetch = new FakeFetch();
  fetch.addFork("vm_policy");
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
      vmPayload(id));
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
  const created = new FakeSandbox("new-box", "blaxel", { metadata: { name: "new-box" }, fs: {}, process: {} });
  await withFakeCompute(created, async (fake) => {
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
  }, { getByIdResult: null });
}

async function testBlaxelInitializationFailureDeletesCreatedVm(): Promise<void> {
  const created = new FakeSandbox("failed-initialization", "arker", { id: "vm-test", _client: { _request: async () => { throw new Error("initialization failed"); } } });
  const fake = installFakeCompute(created);
  try {
    await assert.rejects(BlaxelSandbox.create(), /initialization failed/);
    assert.equal(created.destroyed, true);
    assert.equal(fake.createCalls.length, 1);
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
await testE2BArkerGitRunsOfficialWorkflowAgainstLocalRepository();
await testModalSurface();
await testInternalArkerProvider();
await testInternalArkerProviderImageAndResources();
await testInternalArkerProviderMapsLifetimeToServerSchedule();
await testInternalArkerProviderRejectsUntranslatedPolicyControls();
await testModalEmptyAllowlistIsNotUnrestricted();
await testInvalidNetworkAllowlistsNeverMeanUnrestricted();
await testInternalArkerDirectoryNames();
await testInternalArkerProviderSourceProvenance();
await testSharedForkPolicySecretsAndRegistry();
await testSharedForkParentIdEnvironment();
await testDaytonaResourceUnits();
await testInternalArkerProviderPolicyReplacement();
await testOrderedPolicyControlsSurviveNativeNetworkRestrictions();
await testInternalArkerProviderRequiresSource();
await testBlaxelCreateIfNotExistsAndTtl();
await testBlaxelInitializationFailureDeletesCreatedVm();
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



for (const metadata of [undefined, null, {}, { purpose: "child", nested: { enabled: true, cleared: null } }]) {
  const fetch = new FakeFetch();
  fetch.addFork("metadata-child");
  const provider = createArkerComputeProvider({ apiKey: "test", source: "source", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false });
  await provider.sandbox.create({ metadata } as any);
  const request = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(Object.hasOwn(request, "metadata"), metadata !== undefined);
  if (metadata !== undefined) assert.deepEqual(request.metadata, metadata);
}

{
  const fetch = new FakeFetch();
  fetch.addFork("metadata-child");
  const provider = createArkerComputeProvider({ apiKey: "test", sourceVmId: "resolved-parent", baseUrl: "https://arker.invalid/api", fetch: fetch.fetch, retry: false }, e2bForkMetadata);
  await provider.sandbox.create();
  const request = JSON.parse(fetch.calls[0]!.body!);
  assert.equal(request.source_vm_id, "resolved-parent");
  assert.deepEqual(request.metadata, { arker_e2b: { version: 1, envd_version: "0.10.2", metadata: {}, source: { kind: "vm_id", value: "resolved-parent" } } });
}
console.log("PASS compat");
