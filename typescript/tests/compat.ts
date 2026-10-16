import assert from "node:assert/strict";

import type { CommandResult, FileEntry, RunCommandOptions, SandboxInfo, SandboxInterface } from "computesdk";

import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { SandboxInstance as BlaxelSandbox } from "../src/blaxel.js";
import { ModalClient } from "../src/modal.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";
import { setCompatComputeFactoryForTests } from "../src/compat/runtime.js";

type CapturedComputeConfig = {
  providers: Array<{ name?: string }>;
};

class FakeSandbox implements SandboxInterface {
  readonly instanceId: string;
  readonly sandboxId: string;
  readonly provider = "arker";
  readonly runCalls: Array<{ command: string; options?: RunCommandOptions }> = [];
  readonly createOptions: unknown[] = [];
  destroyed = false;

  constructor(sandboxId = "fake-arker-vm") {
    this.sandboxId = sandboxId;
    this.instanceId = sandboxId;
  }

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
      () => new Daytona({ apiKey: "daytona-key", serverUrl: "unsupported" } as any),
      /unsupported option\(s\) 'serverUrl'/,
    );
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

async function testE2BSurface(): Promise<void> {
  const box = new FakeSandbox("e2b-box");
  const fake = installFakeCompute(box);
  try {
    const sandbox = await E2BSandbox.create("catalog-template", { timeoutMs: 123 });
    assertArkerFirst(fake.captured(), "e2b");
    assert.deepEqual(fake.createCalls[0], { templateId: "catalog-template", timeout: 123 });
    assert.equal(sandbox.sandboxId, "e2b-box");

    const command = await sandbox.commands.run("printf e2b", { cwd: "/tmp", env: { A: "b" }, timeout: 200 });
    assert.deepEqual(command, {
      stdout: "stdout:printf e2b",
      stderr: "stderr:printf e2b",
      exitCode: 7,
    });
    assert.deepEqual(box.runCalls[0]!.options, { cwd: "/tmp", env: { A: "b" }, timeout: 200 });

    assert.equal(await sandbox.files.read("/tmp/a"), "read:/tmp/a");
    await sandbox.files.write("/tmp/a", "content");
    await sandbox.files.makeDir("/tmp/dir");
    assert.deepEqual(await sandbox.files.list("/tmp"), [{ name: "/tmp", type: "file", size: 1 }]);
    assert.equal(await sandbox.files.exists("/tmp/a"), true);
    await sandbox.files.remove("/tmp/a");
    await sandbox.kill();
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

async function testModalSurface(): Promise<void> {
  const box = new FakeSandbox("modal-box");
  const fake = installFakeCompute(box);
  try {
    const client = new ModalClient({ tokenId: "modal-token-id", tokenSecret: "modal-token-secret" });
    const sandbox = await client.sandboxes.create();
    assertArkerFirst(fake.captured(), "modal");
    assert.equal(sandbox.sandboxId, "modal-box");

    const proc = await sandbox.exec(["sh", "-c", "echo modal"], {
      workdir: "/workspace",
      env: { FOO: "bar" },
      timeoutMs: 456,
    });
    assert.equal(await proc.stdout.readText(), "stdout:echo modal");
    assert.equal(await proc.stderr.readText(), "stderr:echo modal");
    assert.equal(await proc.wait(), 7);
    assert.deepEqual(box.runCalls[0], {
      command: "echo modal",
      options: { cwd: "/workspace", env: { FOO: "bar" }, timeout: 456 },
    });

    await sandbox.exec(["node", "script with space.js", "arg"]);
    assert.equal(box.runCalls[1]!.command, "node 'script with space.js' arg");

    await sandbox.terminate();
    assert.equal(box.destroyed, true);

    await assert.rejects(() => client.sandboxes.create("app", "image", {}, "extra"), /at most app, image, and options/);
    await assert.rejects(
      () => client.sandboxes.create("app", "image", { gpu: "H100" }),
      /unsupported option\(s\) 'gpu'/,
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
  await provider.sandbox.create({ image: "ubuntu:24.04", cpu: 4, memory: 8192, diskMiB: 20480, name: "image-box" });
  assert.deepEqual(JSON.parse(fetch.calls[0]!.body!), {
    image: "ubuntu:24.04",
    name: "image-box",
    resources: { vcpu: 4, memory_mib: 8192, disk_mib: 20480 },
  });
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
  assert.deepEqual(body.policies, {
    policies: [
      { type: "outbound", match: { hosts: ["api.example.com"], ports: [443] }, action: "allow" },
      { type: "outbound", match: { ips: ["203.0.113.0/24"] }, action: "allow" },
      { type: "outbound", action: "deny" },
    ],
  });
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
    });
    await assert.rejects(() => BlaxelSandbox.create({ ttl: "eventually" }), /Invalid Blaxel ttl/);
  } finally {
    fake.cleanup();
  }
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
await testDaytonaGetNotFound();
await testE2BSurface();
await testModalSurface();
await testInternalArkerProvider();
await testInternalArkerProviderImageAndResources();
await testInternalArkerProviderSourceProvenance();
await testInternalArkerProviderPolicyReplacement();
await testInternalArkerProviderRequiresSource();
await testBlaxelCreateIfNotExistsAndTtl();

console.log("PASS compat");
