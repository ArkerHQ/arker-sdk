/**
 * Live CLI end-to-end suite.
 *
 * tests/cli.ts drives the CLI against local fake servers; this one drives the
 * built binary against a real deployment, so protocol drift the fakes cannot
 * see (auth shape, error bodies, streaming) fails here.
 *
 *   ARKER_API_KEY=ark_live_... \
 *   ARKER_PROVIDER=aws \
 *   ARKER_REGION=us-west-2 \
 *   ARKER_SOURCE_VM=<source-name> \
 *   bun run test:e2e
 *
 * Not part of `bun run test`: CI has no credentials.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const cliEntry = "dist/cli.js";
const cliRuntime = "node";

const SOURCE_VM = process.env.ARKER_SOURCE_VM;
const MARKER = "hello-from-arker-cli";
const REMOTE_PATH = "/home/user/cli-e2e.txt";

type CliResult = { code: number | null; stdout: string; stderr: string };

async function runCli(args: string[], stdin?: string): Promise<CliResult> {
  const child = spawn(cliRuntime, [cliEntry, ...args], {
    cwd: packageRoot,
    env: { ...process.env, NODE_NO_WARNINGS: "1" },
    stdio: [stdin === undefined ? "ignore" : "pipe", "pipe", "pipe"],
  });
  if (stdin !== undefined) child.stdin!.end(stdin);
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout!.on("data", (c: Buffer) => { stdout.push(c); });
  child.stderr!.on("data", (c: Buffer) => { stderr.push(c); });
  const [code] = (await once(child, "close")) as [number | null];
  return {
    code,
    stdout: Buffer.concat(stdout).toString("utf8"),
    stderr: Buffer.concat(stderr).toString("utf8"),
  };
}

/** Run a command that must succeed; failures print the CLI's own stderr. */
async function ok(args: string[], stdin?: string): Promise<CliResult> {
  const result = await runCli(args, stdin);
  assert.equal(result.code, 0, `arker ${args.join(" ")} exited ${result.code}: ${result.stderr}`);
  return result;
}

async function testWhoamiReachesTheControlPlane(): Promise<void> {
  const { stdout } = await ok(["whoami"]);
  assert.ok(JSON.parse(stdout).org_id, `whoami returned no org_id: ${stdout}`);
}

async function testForkRunSyncAndRemove(): Promise<void> {
  const fork = await ok(["vms", "fork", SOURCE_VM!, "--name", "cli-e2e"]);
  const vmId = JSON.parse(fork.stdout).vm_id as string;
  assert.ok(vmId, `fork returned no vm_id: ${fork.stdout}`);

  try {
    const run = await ok(["run", vmId, `printf '${MARKER}\\n'`]);
    assert.ok(run.stdout.includes(MARKER), `run stdout missing marker: ${run.stdout}`);

    await ok(["sync", vmId, REMOTE_PATH, MARKER]);
    const read = await ok(["sync", vmId, REMOTE_PATH]);
    assert.equal(read.stdout, MARKER);

    const get = await ok(["vms", "get", vmId]);
    assert.ok(get.stdout.includes(vmId), `vms get did not name the VM: ${get.stdout}`);
  } finally {
    await ok(["vms", "rm", vmId]);
  }

  const gone = await runCli(["vms", "get", vmId]);
  assert.notEqual(gone.code, 0, "vms get on a deleted VM should fail");
}

async function testUnauthenticatedRequestFails(): Promise<void> {
  const home = mkdtempSync(join(tmpdir(), "arker-cli-e2e-home-"));
  const child: NodeJS.ProcessEnv = { ...process.env, HOME: home };
  delete child.ARKER_API_KEY;
  try {
  const result = await new Promise<CliResult>((resolve) => {
    const proc = spawn(cliRuntime, [cliEntry, "whoami"], { cwd: packageRoot, env: child, stdio: ["ignore", "pipe", "pipe"] });
    const out: Buffer[] = [];
    const errs: Buffer[] = [];
    proc.stdout!.on("data", (c: Buffer) => { out.push(c); });
    proc.stderr!.on("data", (c: Buffer) => { errs.push(c); });
    proc.on("close", (code) => resolve({ code, stdout: Buffer.concat(out).toString(), stderr: Buffer.concat(errs).toString() }));
  });
  assert.notEqual(result.code, 0, "whoami without an API key should fail");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
}

if (!process.env.ARKER_API_KEY || !SOURCE_VM) {
  throw new Error("set ARKER_API_KEY and ARKER_SOURCE_VM (plus ARKER_PROVIDER + ARKER_REGION or ARKER_BASE_URL)");
}

await testWhoamiReachesTheControlPlane();
await testForkRunSyncAndRemove();
await testUnauthenticatedRequestFails();

console.log("PASS cli-e2e");
