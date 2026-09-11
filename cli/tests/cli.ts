import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer as createHttp1Server, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { createServer as createHttp2Server } from "node:http2";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";

type CliResult = {
  code: number | null;
  stdout: Buffer;
  stderr: string;
  stderrBytes: Buffer;
};

type CliOptions = {
  stdin?: string | Uint8Array;
  authenticated?: boolean;
  controlOnly?: boolean;
};

type CapturedRequest = {
  method: string | undefined;
  url: string | undefined;
  body: unknown;
  idempotencyKey?: string;
};

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const packageVersion = (JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as { version: string }).version;
const cliEntry = "dist/cli.js";
const cliRuntime = "node";

async function withServer(
  handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>,
  fn: (baseUrl: string, server: Server) => Promise<void>,
  options: { http1?: boolean } = {},
): Promise<void> {
  const server = (options.http1
    ? createHttp1Server(handler)
    : createHttp2Server(handler as never)) as Server;
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address === "object");
  try {
    await fn(`http://127.0.0.1:${address.port}/api`, server);
  } finally {
    server.close();
    await once(server, "close");
  }
}

async function withCapturedServer(
  responder: (request: CapturedRequest, res: ServerResponse) => void,
  fn: (baseUrl: string, requests: CapturedRequest[]) => Promise<void>,
  options: { http1?: boolean } = {},
): Promise<void> {
  const requests: CapturedRequest[] = [];
  await withServer(async (req, res) => {
    const request = {
      method: req.method,
      url: req.url,
      body: await readJson(req),
      ...(typeof req.headers["idempotency-key"] === "string"
        ? { idempotencyKey: req.headers["idempotency-key"] }
        : {}),
    };
    requests.push(request);
    res.setHeader("content-type", "application/json");
    responder(request, res);
  }, async (baseUrl) => fn(baseUrl, requests), options);
}

// Every fork carries a generated `Idempotency-Key`, so a captured fork request
// has one more field than the body under test. Asserted here rather than
// stripped: a fork that stops sending a key is exactly the regression the key
// exists to prevent, and dropping the field quietly would let that pass.
/// Fork idempotency is opt-in, so a plain `arker fork` must send no key at all
/// -- an unkeyed fork is never deduplicated, which is the API's own behaviour.
/// Asserted rather than ignored: a key appearing here would mean the CLI had
/// started opting callers in without being asked.
function assertNoForkKeys(requests: CapturedRequest[]): void {
  for (const [index, { idempotencyKey }] of requests.entries()) {
    assert.equal(idempotencyKey, undefined, `request ${index} sent an unrequested Idempotency-Key`);
  }
}

/// The captured requests with the key field dropped, so the rest can be
/// compared against a literal.
function requestsWithoutKeys(requests: CapturedRequest[]): CapturedRequest[] {
  return requests.map(({ idempotencyKey: _key, ...rest }) => rest);
}

async function runCli(baseUrl: string | undefined, args: string[], options: CliOptions = {}): Promise<CliResult> {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    HOME: "/nonexistent/ark-202-cli-test-home",
    NODE_NO_WARNINGS: "1",
  };
  if (options.authenticated !== false) env.ARKER_API_KEY = "ark_live_test";
  else delete env.ARKER_API_KEY;
  if (baseUrl) {
    if (!options.controlOnly) env.ARKER_BASE_URL = baseUrl;
    else delete env.ARKER_BASE_URL;
    env.ARKER_CONTROL_BASE_URL = baseUrl;
  } else {
    delete env.ARKER_BASE_URL;
    delete env.ARKER_CONTROL_BASE_URL;
  }

  const child = spawn(cliRuntime, [cliEntry, ...args], {
    cwd: packageRoot,
    env,
    stdio: [options.stdin === undefined ? "ignore" : "pipe", "pipe", "pipe"],
  });
  if (options.stdin !== undefined) child.stdin!.end(options.stdin);
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout!.on("data", (chunk: Buffer) => { stdout.push(chunk); });
  child.stderr!.on("data", (chunk: Buffer) => { stderr.push(chunk); });
  const [code] = await once(child, "close") as [number | null];
  const stderrBytes = Buffer.concat(stderr);
  return {
    code,
    stdout: Buffer.concat(stdout),
    stderr: stderrBytes.toString("utf8"),
    stderrBytes,
  };
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text) return undefined;
  // Not every request body is JSON any more: /sync-stream sends raw bytes with
  // its parameters in the query string. Capture those as text instead of
  // throwing, so a raw-body request is still assertable.
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function jsonResponse(res: ServerResponse, value: unknown, status = 200): void {
  res.statusCode = status;
  res.end(JSON.stringify(value));
}

function completedRun(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    state: "completed",
    stdout: "",
    stdout_encoding: "utf-8",
    stderr: "",
    stderr_encoding: "utf-8",
    exit_code: 0,
    ...overrides,
  };
}

function stdoutText(result: CliResult): string {
  return result.stdout.toString("utf8");
}

async function testRunOptionsStopAtRemoteCommand(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, [
      "run",
      "--time-to-background",
      "0",
      "--timeout",
      "1000",
      "--end-symbol", "DONE",
      "--vcpu", "2",
      "--memory-mib", "4096",
      "--disk-mib", "8192",
      "--memory-backend", "uffd",
      "--idempotency-key", "run-request-1",
      "vm_1",
      "npm",
      "--version",
    ]);
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(requests, [{
      method: "POST",
      url: "/api/v1/vms/vm_1/runs",
      body: {
        time_to_background: 0,
        timeout: 1000,
        command: "npm --version",
        end_symbol: "DONE",
        vcpu_count: 2,
        memory_mib: 4096,
        disk_mib: 8192,
        memory_backend: "uffd",
      },
      idempotencyKey: "run-request-1",
    }]);
  });
}

async function testKnownFlagAfterRemoteCommandPassesThrough(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "echo", "hello", "--background"]);
    assert.equal(result.code, 0);
    assert.deepEqual(requests[0]?.body, { command: "echo hello --background" });
  });
}

async function testRunOptionAfterVmBeforeCommand(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "--session-idx", "0", "echo", "ok"]);
    assert.equal(result.code, 0);
    assert.deepEqual(requests[0]?.body, { session_idx: 0, command: "echo ok" });
  });
}

async function testRunOptionSeparator(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "--", "printf", "ok"]);
    assert.equal(result.code, 0);
    assert.deepEqual(requests[0]?.body, { command: "printf ok" });
  });
}

async function testRunPreservesArgumentBoundaries(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "printf", "%s", "hello world"]);
    assert.equal(result.code, 0);
    assert.deepEqual(requests[0]?.body, { command: "printf %s 'hello world'" });
  });
}

async function testUnknownRunOptionBeforeCommandFails(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["run", "--backgroun", "vm_1", "echo", "ok"]);
    assert.equal(result.code, 1);
    assert.equal(requests.length, 0);
    assert.match(result.stderr, /unknown parameter "backgroun"/);
  });
}

async function testRemovedBackgroundOptionFailsBeforeRequest(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["run", "--background", "vm_1", "echo", "ok"]);
    assert.equal(result.code, 1);
    assert.equal(requests.length, 0);
    assert.match(result.stderr, /unknown parameter "background"/);
  });
}

async function testNestedRunStopsAtRemoteCommand(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["vms", "run", "--time-to-background", "0", "vm_1", "npm", "--version"]);
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(requests[0]?.body, { time_to_background: 0, command: "npm --version" });
  });
}

async function testKnownButIrrelevantNestedOptionsFail(): Promise<void> {
  for (const args of [
    ["vms", "get", "--timeout", "1", "vm_1"],
    ["runs", "get", "--state", "failed", "vm_1", "run_1"],
    ["sessions", "get", "--cwd", "/tmp", "vm_1", "session_1"],
    ["mounts", "rm", "--path", "/mnt", "vm_1", "sync_1"],
    ["fs", "get", "--name", "data", "fs_1"],
  ]) {
    await withCapturedServer((_request, res) => jsonResponse(res, {}), async (baseUrl, requests) => {
      const result = await runCli(baseUrl, args);
      assert.equal(result.code, 1, args.join(" "));
      assert.equal(requests.length, 0, args.join(" "));
      assert.match(result.stderr, /is not valid for/, args.join(" "));
    });
  }
}

async function testInvalidNumbersFailBeforeRequest(): Promise<void> {
  for (const { args, flag } of [
    { args: ["run", "--timeout", "wat", "vm_1", "echo", "ok"], flag: "timeout" },
    { args: ["run", "--session-idx", "-1", "vm_1", "echo", "ok"], flag: "session-idx" },
    { args: ["run", "--time-to-background", "1.5", "vm_1", "echo", "ok"], flag: "time-to-background" },
    { args: ["ls", "--limit", "1001"], flag: "limit" },
    { args: ["fork", "--vcpu", "256", "source-vm"], flag: "vcpu" },
    { args: ["update", "--memory-mib", "Infinity", "vm_1"], flag: "memory-mib" },
    { args: ["shell", "--rows", "0", "vm_1"], flag: "rows" },
    { args: ["run", "--timeout=", "vm_1", "echo", "ok"], flag: "timeout" },
    { args: ["run", "--timeout= ", "vm_1", "echo", "ok"], flag: "timeout" },
  ]) {
    await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
      const result = await runCli(baseUrl, args);
      assert.equal(result.code, 1);
      assert.equal(requests.length, 0);
      assert.match(result.stderr, new RegExp(flag));
    });
  }
}

/// `--vgpu` is the only fractional resource flag, so it exercises the number
/// option type as well as the fork wiring — an integer-only parser would have
/// refused `0.25` outright.
async function testForkForwardsVgpu(): Promise<void> {
  await withCapturedServer(
    (_request, res) => jsonResponse(res, { vm_id: "vm_gpu" }),
    async (baseUrl, requests) => {
      const result = await runCli(baseUrl, ["fork", "--vgpu", "0.25", "source-vm"]);

      assert.equal(result.code, 0, result.stderr);
      assert.equal(
        (requests[0]!.body as { resources: { vgpu: number } }).resources.vgpu,
        0.25,
      );
    },
  );

  // Off the ladder, out of range, and non-numeric must all fail before any
  // request is made — the server enforces eighths, so spending a round trip to
  // be told so is pure latency.
  for (const value of ["0", "1.5", "-0.5", "half", "0.3", "0.2", "0.0625"]) {
    await withCapturedServer(
      (_request, res) => jsonResponse(res, { vm_id: "vm_gpu" }),
      async (baseUrl, requests) => {
        const result = await runCli(baseUrl, ["fork", "--vgpu", value, "source-vm"]);
        assert.equal(result.code, 1, `--vgpu ${value} must be refused`);
        assert.equal(requests.length, 0, `--vgpu ${value} must not reach the server`);
      },
    );
  }
}

/// `ResourcesInput` is `deny_unknown_fields`, so an UNKNOWN key fails the fork
/// even when its value is null. The retired per-card fields were sent as nulls
/// on every fork, which is why `--vcpu` alone was rejected too; a null on a
/// known field is fine and still means "inherit from the source".
async function testForkOmitsRetiredGpuResourceKeys(): Promise<void> {
  await withCapturedServer(
    (_request, res) => jsonResponse(res, { vm_id: "vm_gpu" }),
    async (baseUrl, requests) => {
      const result = await runCli(baseUrl, [
        "fork",
        "--platform",
        "x86_64-l40s",
        "--vgpu",
        "0.25",
        "source-vm",
      ]);

      assert.equal(result.code, 0, result.stderr);
      assertNoForkKeys(requests);
      assert.deepEqual(requestsWithoutKeys(requests), [{
        method: "POST",
        url: "/api/v1/fork",
        body: {
          source_vm_name: "source-vm",
          platforms: ["x86_64-l40s"],
          resources: { vgpu: 0.25 },
        },
      }]);
    },
  );
}

async function testForkForwardsImageOptionsAndRedactsSecrets(): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "arker-cli-fork-"));
  const registryAuthFile = join(dir, "registry.json");
  const sshKeysFile = join(dir, "authorized_keys");
  const policiesFile = join(dir, "policies.json");
  const password = "registry-\"secret\\next\nline";
  const policySecret = "policy-\"secret\\next\nline";
  const policies = {
    policies: [{ direction: "outbound", action: "allow", protocol: "tcp", port: 443 }],
    secrets: { API_TOKEN: policySecret },
  };
  writeFileSync(registryAuthFile, JSON.stringify({ username: "registry-user", password }));
  writeFileSync(sshKeysFile, "ssh-ed25519 AAAAfile file@example\n\n");
  writeFileSync(policiesFile, JSON.stringify(policies));

  try {
    await withCapturedServer(
      (_request, res) => jsonResponse(res, { vm_id: "vm_image" }),
      async (baseUrl, requests) => {
        const result = await runCli(baseUrl, [
          "fork",
          "--image", "ghcr.io/example/private:v1",
          "--registry-auth-file", registryAuthFile,
          "--ssh-public-key", "ssh-ed25519 AAAAone one@example",
          "--ssh-public-key", "ssh-rsa AAAAtwo two@example",
          "--ssh-public-keys-file", sshKeysFile,
          "--durable",
          "--nestedvirt",
          "--policies-file", policiesFile,
        ]);
        assert.equal(result.code, 0, result.stderr);
        assertNoForkKeys(requests);
        assert.deepEqual(requestsWithoutKeys(requests), [{
          method: "POST",
          url: "/api/v1/fork",
          body: {
            image: "ghcr.io/example/private:v1",
            ssh_public_keys: [
              "ssh-ed25519 AAAAone one@example",
              "ssh-rsa AAAAtwo two@example",
              "ssh-ed25519 AAAAfile file@example",
            ],
            durable: true,
            nestedvirt: true,
            policies,
            registry_auth: { username: "registry-user", password },
          },
        }]);
      },
    );

    await withCapturedServer(
      (_request, res) => jsonResponse(res, {
        code: "bad_request",
        message: `credentials ${password} ${policySecret}`,
      }, 400),
      async (baseUrl) => {
        const result = await runCli(baseUrl, [
          "fork", "--image", "private.example/image:v1",
          "--registry-auth-file", registryAuthFile,
          "--policies-file", policiesFile,
        ]);
        assert.equal(result.code, 1);
        for (const secret of [password, policySecret]) {
          assert.ok(!result.stderr.includes(secret));
          assert.ok(!result.stderr.includes(JSON.stringify(secret).slice(1, -1)));
        }
      },
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function testForkUsesDockerfileAndContext(): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "arker-cli-dockerfile-"));
  const dockerfile = join(dir, "Dockerfile");
  writeFileSync(dockerfile, "FROM ubuntu:24.04\n");
  try {
    await withCapturedServer(
      (_request, res) => jsonResponse(res, { vm_id: "vm_built" }),
      async (baseUrl, requests) => {
        const result = await runCli(baseUrl, ["fork", "--dockerfile", dockerfile, "--context", dir]);
        assert.equal(result.code, 0, result.stderr);
        assertNoForkKeys(requests);
        assert.deepEqual(requestsWithoutKeys(requests), [{
          method: "POST",
          url: "/api/v1/fork",
          body: { image: "ubuntu:24.04" },
        }]);
      },
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function testOrgRunListForwardsFilters(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {
    since: 10, until: 20, limit: 100, offset: 5, lite: true, rows: [],
  }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, [
      "runs", "ls",
      "--since", "10",
      "--until", "20",
      "--vm", "vm_1",
      "--vms", "vm_2,vm_3",
      "--region", "us-west-2",
      "--provider", "aws",
      "--search", "pytest",
      "--limit", "100",
      "--offset", "5",
      "--lite",
      "--runtime", "fc",
      "--endpoint", "run",
      "--actions", "run,fork",
      "--status", "success,internal",
      "--status-min", "200",
      "--status-max", "599",
      "--sort", "when",
      "--dir", "asc",
      "--json",
    ], { controlOnly: true });
    assert.equal(result.code, 0, result.stderr);
    const url = new URL(requests[0]?.url ?? "", "http://localhost");
    assert.equal(url.pathname, "/api/v1/runs");
    assert.deepEqual(Object.fromEntries(url.searchParams), {
      since: "10",
      until: "20",
      vm: "vm_1",
      vms: "vm_2,vm_3",
      region: "us-west-2",
      provider: "aws",
      search: "pytest",
      limit: "100",
      offset: "5",
      lite: "true",
      runtime: "fc",
      endpoint: "run",
      actions: "run,fork",
      status: "success,internal",
      status_min: "200",
      status_max: "599",
      sort: "when",
      dir: "asc",
    });
  });
}

async function testGlobalOptionsBeforeCommand(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, { vms: [] }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["--region", "us-west-2", "ls"]);
    assert.equal(result.code, 0);
    assert.equal(requests.length, 1);
    assert.match(requests[0]?.url ?? "", /region=us-west-2/);
  });
}

async function testHelpAndVersionAreLocalSuccesses(): Promise<void> {
  for (const args of [["--help"], ["-h"], ["run", "--help"]]) {
    const result = await runCli(undefined, args, { authenticated: false });
    assert.equal(result.code, 0, `${args.join(" ")} should succeed`);
    assert.match(stdoutText(result), /Usage:/);
    assert.equal(result.stderr, "");
  }
  for (const args of [["--version"], ["-v"]]) {
    const result = await runCli(undefined, args, { authenticated: false });
    assert.equal(result.code, 0, `${args.join(" ")} should succeed`);
    assert.equal(stdoutText(result), `arker ${packageVersion}\n`);
    assert.equal(result.stderr, "");
  }
}

async function testArbitraryProviderFlagIsAccepted(): Promise<void> {
  const result = await runCli(
    undefined,
    ["--provider", "future-cloud", "--region", "moon-1", "--help"],
    { authenticated: false },
  );
  assert.equal(result.code, 0, result.stderr);
}

async function testComputeCommandRequiresProviderAndRegion(): Promise<void> {
  const result = await runCli(
    undefined,
    ["update", "vm_1", "--provider", "provider-one"],
  );

  assert.equal(result.code, 1);
  assert.match(result.stderr, /provider and region are required for compute commands/i);
}

async function testProviderOnlyVmListDoesNotRequireAComputeRegion(): Promise<void> {
  await withCapturedServer(
    (_request, res) => jsonResponse(res, { vms: [] }),
    async (baseUrl, requests) => {
      const result = await runCli(baseUrl, [
        "--provider", "provider-one",
        "list",
        "--platform", "x86_64-l40s",
        "--created-after", "2026-01-01T00:00:00Z",
        "--created-before", "2026-02-01T00:00:00Z",
      ], {
        controlOnly: true,
      });

      assert.equal(result.code, 0, result.stderr);
      assert.equal(requests.length, 1);
      assert.equal(
        requests[0]?.url,
        "/api/v1/vms?provider=provider-one&platform=x86_64-l40s&created_after=2026-01-01T00%3A00%3A00Z&created_before=2026-02-01T00%3A00%3A00Z",
      );
    },
  );
}

async function testRegionsDiscoveryNeedsNoCredentialsOrPlacement(): Promise<void> {
  const placement = {
    provider: "provider-one",
    region: "region-one",
  };
  await withCapturedServer(
    (_request, res) => jsonResponse(res, { regions: [placement] }),
    async (baseUrl, requests) => {
      const human = await runCli(baseUrl, ["regions"], {
        authenticated: false,
      });
      assert.equal(human.code, 0, human.stderr);
      assert.equal(stdoutText(human), "provider-one-region-one\n");

      const json = await runCli(baseUrl, ["regions", "--json"], {
        authenticated: false,
      });
      assert.equal(json.code, 0, json.stderr);
      assert.deepEqual(JSON.parse(stdoutText(json)), { regions: [placement] });
      assert.deepEqual(
        requests.map((request) => [request.method, request.url]),
        [
          ["GET", "/api/v1/regions"],
          ["GET", "/api/v1/regions"],
        ],
      );
    },
  );
}

async function testWhoamiUsesAuthenticatedControlPlane(): Promise<void> {
  await withCapturedServer(
    (_request, res) => jsonResponse(res, { org_id: "org_01", org_name: "ArkerHQ" }),
    async (baseUrl, requests) => {
      const result = await runCli(baseUrl, ["whoami"], { controlOnly: true });
      assert.equal(result.code, 0, result.stderr);
      assert.deepEqual(JSON.parse(stdoutText(result)), { org_id: "org_01", org_name: "ArkerHQ" });
      assert.deepEqual(requests.map(({ method, url }) => [method, url]), [["GET", "/api/v1/whoami"]]);
    },
  );
}

async function testRemovedSecretAndUrlFlagsFailLocally(): Promise<void> {
  for (const args of [
    ["--api-key", "secret", "ls"],
    ["--base-url", "http://localhost", "ls"],
    ["--control-base-url", "http://localhost", "ls"],
  ]) {
    const result = await runCli(undefined, args);
    assert.equal(result.code, 1);
    assert.match(result.stderr, /unknown parameter/);
  }
}

async function testPerCommandHelpIsCommandSpecific(): Promise<void> {
  const global = stdoutText(await runCli(undefined, ["--help"], { authenticated: false }));

  // Every command must render its own help, not the global blob.
  for (const command of [
    "delete", "filesystems", "fork", "fs", "list", "ls", "policies", "regions",
    "rm", "run", "runs", "sessions", "shell", "signal", "sync", "sync-dir",
    "mounts", "update", "vms", "whoami",
  ]) {
    const result = await runCli(undefined, [command, "--help"], { authenticated: false });
    assert.equal(result.code, 0, `${command} --help should succeed`);
    const help = stdoutText(result);
    assert.match(help, /Usage:/);
    assert.notEqual(help, global, `${command} --help must not be the global help`);
    assert.match(help, /Run 'arker --help' for the full command list\./);
  }

  // The flag list is read from COMMAND_OPTIONS, so a command's own options show up.
  const ls = stdoutText(await runCli(undefined, ["ls", "--help"], { authenticated: false }));
  for (const flag of ["--region", "--provider", "--limit", "--cursor", "--state", "--public"]) {
    assert.ok(ls.includes(flag), `ls --help should document ${flag}`);
  }
  assert.doesNotMatch(ls, /--vgpu|--no-disk/, "ls --help must not list fork-only flags");

  // A recognised subcommand is called out by name.
  const vmsLs = stdoutText(await runCli(undefined, ["vms", "ls", "--help"], { authenticated: false }));
  assert.match(vmsLs, /Subcommand 'ls'/);
}

async function testHelpMatchesSupportedSurface(): Promise<void> {
  const result = await runCli(undefined, ["--help"], { authenticated: false });
  const help = stdoutText(result);
  assert.doesNotMatch(help, /network overrides/);
  assert.doesNotMatch(help, /--api-key|--base-url|--control-base-url|ssh-keys/);
  assert.match(help, /CLI options must appear before <command>/);
}

async function testRunJsonIncludesMemoryMetadata(): Promise<void> {
  let body: unknown;
  await withServer(async (req, res) => {
    body = await readJson(req);
    res.setHeader("content-type", "application/json");
    jsonResponse(res, completedRun({
      stdout: "hello\n",
      stderr: "warning\0",
      memory_requested_mib: 1024,
      memory_achieved_mib: 1536,
      memory_partial: true,
    }));
  }, async (baseUrl) => {
    const result = await runCli(baseUrl, ["run", "--json", "vm_1", "echo", "hello"]);
    assert.equal(result.code, 0);
    assert.equal(result.stderr, "");
    assert.deepEqual(body, { command: "echo hello" });
    const payload = JSON.parse(stdoutText(result));
    assert.equal(payload.stdout, "aGVsbG8K");
    assert.equal(payload.stdoutEncoding, "base64");
    assert.equal(payload.stderr, "d2FybmluZwA=");
    assert.equal(payload.stderrEncoding, "base64");
    assert.equal(payload.memoryRequestedMib, 1024);
    assert.equal(payload.memoryAchievedMib, 1536);
    assert.equal(payload.memoryPartial, true);
  });
}

async function testRunHumanWritesArbitraryBytes(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun({
    stdout: "/wD+",
    stdout_encoding: "base64",
    stderr: "gQD/",
    stderr_encoding: "base64",
  })), async (baseUrl) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "binary"]);
    assert.equal(result.code, 0);
    assert.deepEqual(result.stdout, Buffer.from([0xff, 0x00, 0xfe]));
    assert.deepEqual(result.stderrBytes, Buffer.from([0x81, 0x00, 0xff]));
  });
}

async function testRunFailureReasonIsVisible(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun({
    state: "failed",
    exit_code: 1,
    fail_reason: "host disappeared",
  })), async (baseUrl) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "false"]);
    assert.equal(result.code, 1);
    assert.equal(stdoutText(result), "");
    assert.equal(result.stderr, "arker: host disappeared\n");
  });
}

async function testPoliciesGetAndSet(): Promise<void> {
  const doc = { policies: [{ action: "allow", domains: ["example.com"] }], mitm_domains: [] };

  await withCapturedServer((_request, res) => jsonResponse(res, doc), async (baseUrl) => {
    const got = await runCli(baseUrl, ["policies", "get", "vm_1"]);
    assert.equal(got.code, 0);
    assert.deepEqual(JSON.parse(stdoutText(got)), doc);
  });

  // `set` reads the document from stdin and PUTs it verbatim.
  await withCapturedServer((_request, res) => jsonResponse(res, { ok: true }), async (baseUrl, requests) => {
    const set = await runCli(baseUrl, ["policies", "set", "vm_1"], { stdin: JSON.stringify(doc) });
    assert.equal(set.code, 0);
    const put = requests.find((r) => r.method === "PUT");
    assert.ok(put, "expected a PUT to the policies endpoint");
    assert.ok(put!.url!.includes("/policies"), `unexpected url: ${put!.url}`);
    assert.deepEqual(put!.body, doc);
  });
}

async function testPoliciesSetRejectsInvalidJson(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, { ok: true }), async (baseUrl) => {
    const bad = await runCli(baseUrl, ["policies", "set", "vm_1"], { stdin: "{not json" });
    assert.notEqual(bad.code, 0);
    assert.match(bad.stderr, /not valid JSON/);

    const arr = await runCli(baseUrl, ["policies", "set", "vm_1"], { stdin: "[1,2]" });
    assert.notEqual(arr.code, 0);
    assert.match(arr.stderr, /must be a JSON object/);
  });
}

async function testRunsGetUsesRunFormatter(): Promise<void> {
  const response = completedRun({
    run_id: "run_1",
    stdout: "/wD+",
    stdout_encoding: "base64",
    stderr: "",
    stderr_encoding: "utf-8",
  });
  await withCapturedServer((_request, res) => jsonResponse(res, response), async (baseUrl) => {
    const human = await runCli(baseUrl, ["runs", "get", "vm_1", "run_1"]);
    assert.equal(human.code, 0);
    assert.deepEqual(human.stdout, Buffer.from([0xff, 0x00, 0xfe]));

    const json = await runCli(baseUrl, ["runs", "--json", "get", "vm_1", "run_1"]);
    assert.equal(json.code, 0);
    const payload = JSON.parse(stdoutText(json));
    assert.equal(payload.stdout, "/wD+");
    assert.equal(payload.stdoutEncoding, "base64");
  });
}

async function testRunHumanWarnsOnPartialMemory(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun({
    stdout: "hello\n",
    memory_requested_mib: 1024,
    memory_achieved_mib: 1536,
    memory_partial: true,
  })), async (baseUrl) => {
    const result = await runCli(baseUrl, ["run", "vm_1", "echo", "hello"]);
    assert.equal(result.code, 0);
    assert.equal(stdoutText(result), "hello\n");
    assert.match(result.stderr, /Memory target partially applied: requested 1024 MiB, achieved 1536 MiB\./);
  });
}

async function testEmptyPipedInputWritesZeroBytes(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {
    results: [{ complete: true, written: true }],
  }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sync", "vm_1", "/tmp/example.txt"], { stdin: "" });
    assert.equal(result.code, 0);
    assert.equal(stdoutText(result), "wrote 0 bytes to /tmp/example.txt\n");
    // `sync` now streams the bytes to /sync-stream rather than base64-ing them
    // into a JSON `writes[]` envelope: path and size ride in the query string
    // and the body is the raw bytes (here, none). The behaviour asserted above
    // — zero bytes written, exit 0 — is unchanged; only the transport moved.
    assert.equal(requests.length, 1);
    assert.equal(requests[0]?.method, "POST");
    assert.equal(requests[0]?.url, "/api/v1/vms/vm_1/sync-stream?path=%2Ftmp%2Fexample.txt&size=0");
  }, { http1: true });
}

// `-` is the explicit "write stdin" form. It exists so a caller never has to
// rely on stdin sniffing, which cannot distinguish an idle inherited pipe from
// a producer that has not written yet.
async function testSyncDashWritesStdin(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {
    results: [{ complete: true, written: true }],
  }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sync", "vm_1", "/tmp/a.txt", "-"], { stdin: "hi\n" });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(stdoutText(result), "wrote 3 bytes to /tmp/a.txt\n");
    assert.equal(requests[0]?.url, "/api/v1/vms/vm_1/sync-stream?path=%2Ftmp%2Fa.txt&size=3");
  }, { http1: true });
}

// --read is the explicit "read" form: it must win even when data is piped in,
// so a script can force the direction rather than inherit it from its parent.
async function testSyncReadFlagIgnoresPipedStdin(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {
    content: "aGVsbG8=",
    encoding: "base64",
  }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sync", "vm_1", "/tmp/a.txt", "--read"], { stdin: "ignored" });
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(result.stdout, Buffer.from("hello"));
    assert.deepEqual(requests, [{
      method: "POST",
      url: "/api/v1/vms/vm_1/sync",
      body: { op: "read", path: "/tmp/a.txt" },
    }]);
  });
}

async function testSyncReadFlagRejectsDataArgument(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {}), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sync", "vm_1", "/tmp/a.txt", "data", "--read"]);
    assert.equal(result.code, 1);
    assert.match(result.stderr, /--read takes no data argument/);
    assert.equal(requests.length, 0, "must fail before touching the network");
  });
}

async function testSignalValidatesNameBeforeRequest(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["signal", "vm_1", "SIGBOGUS"]);
    assert.equal(result.code, 1);
    assert.match(result.stderr, /unknown signal SIGBOGUS/);
    assert.equal(requests.length, 0, "must fail before touching the network");
  });
}

async function testSignalForwardsSignalAndSession(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, completedRun()), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["signal", "vm_1", "sigterm", "--session-id", "s_1"]);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(requests[0]?.url, "/api/v1/vms/vm_1/runs");
    assert.deepEqual(requests[0]?.body, { signal: "SIGTERM", session_id: "s_1" });
  });
}

async function testSessionsUpdateForwardsPatch(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, { ok: true, session_id: "s_1" }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sessions", "update", "vm_1", "s_1", "--cols", "120", "--timeout-secs", "600"]);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(requests[0]?.method, "PATCH");
    assert.equal(requests[0]?.url, "/api/v1/vms/vm_1/sessions/s_1");
    assert.deepEqual(requests[0]?.body, { cols: 120, timeout_secs: 600 });
  });
}

async function testSessionsUpdateRequiresAField(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {}), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sessions", "update", "vm_1", "s_1"]);
    assert.equal(result.code, 1);
    assert.match(result.stderr, /at least one of --cols, --rows, --timeout-secs/);
    assert.equal(requests.length, 0, "must fail before touching the network");
  });
}

async function testNoPipeReadsFileBytes(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {
    content: "/wD+",
    encoding: "base64",
  }), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["sync", "vm_1", "/tmp/example.bin"]);
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(result.stdout, Buffer.from([0xff, 0x00, 0xfe]));
    assert.deepEqual(requests, [{
      method: "POST",
      url: "/api/v1/vms/vm_1/sync",
      body: { op: "read", path: "/tmp/example.bin" },
    }]);
  }, { http1: true });
}

async function testShellSetupUsesPackagedCli(): Promise<void> {
  const requests: CapturedRequest[] = [];
  const wss = new WebSocketServer({ noServer: true });
  let upgradeUrl: string | undefined;
  let upgradeAuthorization: string | undefined;
  await withServer(async (req, res) => {
    requests.push({ method: req.method, url: req.url, body: await readJson(req) });
    res.setHeader("content-type", "application/json");
    jsonResponse(res, { vm_id: "vm_1", state: "idle" });
  }, async (baseUrl, server) => {
    server.on("upgrade", (req, socket, head) => {
      upgradeUrl = req.url;
      upgradeAuthorization = req.headers.authorization;
      wss.handleUpgrade(req, socket, head, () => {});
    });
    const result = await runCli(baseUrl, ["shell", "--session-id", "session_1", "vm_1"]);
    assert.equal(result.code, 0, result.stderr);
  }, { http1: true });
  assert.deepEqual(requests, [{ method: "GET", url: "/api/v1/vms/vm_1", body: undefined }]);
  assert.match(upgradeUrl ?? "", /^\/api\/v1\/vms\/vm_1\/sessions\/session_1\/pty\?/);
  assert.equal(upgradeAuthorization, "Bearer ark_live_test");
  wss.close();
}

async function testShellRequiresVmOrSourceBeforeRequest(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {}), async (baseUrl, requests) => {
    const result = await runCli(baseUrl, ["shell"]);
    assert.equal(result.code, 1);
    assert.equal(requests.length, 0);
    assert.match(result.stderr, /--source-vm-name/);
  });
}

async function testStructuredErrorsDoNotRepeatCode(): Promise<void> {
  await withCapturedServer((_request, res) => jsonResponse(res, {
    error: {
      code: "not_found",
      message: "VM missing",
      timestamp: "2026-07-21T00:00:00Z",
    },
  }, 404), async (baseUrl) => {
    const result = await runCli(baseUrl, ["run", "vm_missing", "true"]);
    assert.equal(result.code, 1);
    assert.equal(result.stderr, "arker: not_found: VM missing\n");
  });
}

async function testFalseMutationResultsExitNonzero(): Promise<void> {
  const cases = [
    { args: ["runs", "rm", "vm_1", "run_1"], response: { cancelled: false } },
    { args: ["sessions", "rm", "vm_1", "session_1"], response: { deleted: false } },
    { args: ["mounts", "rm", "vm_1", "sync_1"], response: { deleted: false } },
    { args: ["fs", "rm", "fs_1"], response: { deleted: false } },
  ];
  for (const testCase of cases) {
    await withCapturedServer((_request, res) => jsonResponse(res, testCase.response), async (baseUrl) => {
      const result = await runCli(baseUrl, testCase.args);
      assert.equal(result.code, 1, testCase.args.join(" "));
      assert.match(result.stderr, /failed/);
    });
  }
}

async function testRemainingHttpCommandSurface(): Promise<void> {
  const cases: Array<{
    name: string;
    args: string[];
    response: unknown;
    method: string;
    url: string;
    body?: unknown;
    http1?: boolean;
  }> = [
    {
      name: "vms get",
      args: ["vms", "get", "vm_1"],
      response: { vm_id: "vm_1", state: "idle" },
      method: "GET",
      url: "/api/v1/vms/vm_1",
    },
    {
      name: "vms rm",
      args: ["vms", "rm", "vm_1"],
      response: { deleted: true },
      method: "DELETE",
      url: "/api/v1/vms/vm_1",
    },
    {
      name: "vms fork",
      args: [
        "vms",
        "fork",
        "--source-vm-name", "public-template",
        "--source-org-name", "ArkerHQ",
        "--description", "CI runner",
        "--layers", "disk",
        "--disk",
      ],
      response: { vm_id: "vm_child" },
      method: "POST",
      url: "/api/v1/fork",
      body: {
        source_vm_name: "public-template",
        source_org_name: "ArkerHQ",
        description: "CI runner",
        disk: true,
        layers: ["disk"],
      },
    },
    {
      name: "vms update",
      args: [
        "vms",
        "update",
        "--description",
        "Release runner",
        "--memory-mib",
        "1024",
        "--vgpu",
        "0.25",
        "--ssh-public-key",
        "ssh-ed25519 AAAAexample user@example",
        "vm_1",
      ],
      response: { vm_id: "vm_1", state: "idle", memory_mib: 1024 },
      method: "PATCH",
      url: "/api/v1/vms/vm_1",
      body: {
        description: "Release runner",
        resources: { vcpu: null, memory_mib: 1024, disk_mib: null, vgpu: 0.25 },
        ssh_public_keys: ["ssh-ed25519 AAAAexample user@example"],
      },
    },
    {
      name: "runs ls",
      args: [
        "runs", "ls", "vm_1",
        "--started-after", "2026-01-01T00:00:00Z",
        "--started-before", "2026-02-01T00:00:00Z",
        "--completed-after", "2026-01-02T00:00:00Z",
      ],
      response: { runs: [] },
      method: "GET",
      url: "/api/v1/vms/vm_1/runs?started_after=2026-01-01T00%3A00%3A00Z&started_before=2026-02-01T00%3A00%3A00Z&completed_after=2026-01-02T00%3A00%3A00Z",
    },
    {
      name: "sessions ls",
      args: ["sessions", "ls", "vm_1"],
      response: { sessions: [] },
      method: "GET",
      url: "/api/v1/vms/vm_1/sessions",
    },
    {
      name: "sessions get",
      args: ["sessions", "get", "vm_1", "session_1"],
      response: { session_id: "session_1", state: "idle", cwd: "/" },
      method: "GET",
      url: "/api/v1/vms/vm_1/sessions/session_1",
    },
    {
      name: "sessions create",
      args: [
        "sessions", "create", "vm_1",
        "--env", "MODE=test",
        "--cwd", "/work",
        "--pty",
        "--cols", "120",
        "--rows", "40",
        "--command", "/bin/zsh",
      ],
      response: { session_id: "session_1", state: "idle", cwd: "/work" },
      method: "POST",
      url: "/api/v1/vms/vm_1/sessions",
      body: {
        env: { MODE: "test" },
        cwd: "/work",
        pty: true,
        cols: 120,
        rows: 40,
        command: "/bin/zsh",
      },
    },
    {
      name: "mounts ls",
      args: ["mounts", "ls", "vm_1"],
      response: { mounts: [] },
      method: "GET",
      url: "/api/v1/vms/vm_1/mounts",
    },
    {
      name: "mounts create",
      args: ["mounts", "create", "--filesystem-id", "fs_1", "--path", "/mnt", "vm_1"],
      response: { mount_id: "sync_1", filesystem_id: "fs_1", path: "/mnt" },
      method: "POST",
      url: "/api/v1/vms/vm_1/mounts",
      body: { filesystem_id: "fs_1", path: "/mnt" },
    },
    {
      // `sync` streams the bytes now: path and size ride in the query string
      // and the body is raw, rather than base64 inside a JSON `writes[]`.
      name: "sync streamed write",
      http1: true,
      args: ["sync", "vm_1", "/tmp/file", "hello"],
      response: { results: [{ complete: true, written: true }] },
      method: "POST",
      url: "/api/v1/vms/vm_1/sync-stream?path=%2Ftmp%2Ffile&size=5",
      body: "hello",
    },
    {
      name: "filesystems ls",
      args: ["fs", "ls"],
      response: { filesystems: [] },
      method: "GET",
      url: "/api/v1/filesystems",
    },
    {
      name: "filesystems create",
      args: ["fs", "create", "--name", "data"],
      response: { filesystem_id: "fs_1", name: "data" },
      method: "POST",
      url: "/api/v1/filesystems",
      body: { name: "data" },
    },
    {
      name: "filesystems get",
      args: ["fs", "get", "fs_1"],
      response: { filesystem_id: "fs_1", name: "data" },
      method: "GET",
      url: "/api/v1/filesystems/fs_1",
    },
  ];

  for (const testCase of cases) {
    await withCapturedServer((_request, res) => jsonResponse(res, testCase.response), async (baseUrl, requests) => {
      const result = await runCli(baseUrl, testCase.args);
      assert.equal(result.code, 0, `${testCase.name}: ${result.stderr}`);
      assert.equal(requests.length, 1, testCase.name);
      assert.equal(requests[0]?.method, testCase.method, testCase.name);
      assert.equal(requests[0]?.url, testCase.url, testCase.name);
      if (testCase.body !== undefined) assert.deepEqual(requests[0]?.body, testCase.body, testCase.name);
    }, { http1: testCase.http1 });
  }
}

await testRunOptionsStopAtRemoteCommand();
await testKnownFlagAfterRemoteCommandPassesThrough();
await testRunOptionAfterVmBeforeCommand();
await testRunOptionSeparator();
await testRunPreservesArgumentBoundaries();
await testUnknownRunOptionBeforeCommandFails();
await testRemovedBackgroundOptionFailsBeforeRequest();
await testNestedRunStopsAtRemoteCommand();
await testKnownButIrrelevantNestedOptionsFail();
await testInvalidNumbersFailBeforeRequest();
await testForkOmitsRetiredGpuResourceKeys();
await testForkForwardsVgpu();
await testForkForwardsImageOptionsAndRedactsSecrets();
await testForkUsesDockerfileAndContext();
await testOrgRunListForwardsFilters();
await testGlobalOptionsBeforeCommand();
await testHelpAndVersionAreLocalSuccesses();
await testArbitraryProviderFlagIsAccepted();
await testComputeCommandRequiresProviderAndRegion();
await testProviderOnlyVmListDoesNotRequireAComputeRegion();
await testRegionsDiscoveryNeedsNoCredentialsOrPlacement();
await testWhoamiUsesAuthenticatedControlPlane();
await testRemovedSecretAndUrlFlagsFailLocally();
await testHelpMatchesSupportedSurface();
await testPerCommandHelpIsCommandSpecific();
await testRunJsonIncludesMemoryMetadata();
await testRunHumanWritesArbitraryBytes();
await testRunFailureReasonIsVisible();
await testRunsGetUsesRunFormatter();
await testRunHumanWarnsOnPartialMemory();
await testEmptyPipedInputWritesZeroBytes();
await testSyncDashWritesStdin();
await testSyncReadFlagIgnoresPipedStdin();
await testSyncReadFlagRejectsDataArgument();
await testSignalValidatesNameBeforeRequest();
await testSignalForwardsSignalAndSession();
await testSessionsUpdateForwardsPatch();
await testSessionsUpdateRequiresAField();
await testNoPipeReadsFileBytes();
await testShellSetupUsesPackagedCli();
await testShellRequiresVmOrSourceBeforeRequest();
await testStructuredErrorsDoNotRepeatCode();
await testFalseMutationResultsExitNonzero();
await testRemainingHttpCommandSurface();
await testPoliciesGetAndSet();
await testPoliciesSetRejectsInvalidJson();

console.log("PASS cli");
