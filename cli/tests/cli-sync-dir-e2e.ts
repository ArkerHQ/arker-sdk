import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Arker, type VM } from "@arker-ai/sdk";

const source = process.env.ARKER_SOURCE_VM;
assert.ok(source && process.env.ARKER_API_KEY, "set ARKER_API_KEY and ARKER_SOURCE_VM plus placement");
const packageRoot = fileURLToPath(new URL("..", import.meta.url));
async function cli(args: string[]): Promise<string> {
  const child = spawn("node", ["dist/cli.js", ...args], { cwd: packageRoot, stdio: ["ignore", "pipe", "pipe"] });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (bytes: Buffer) => stdout.push(bytes));
  child.stderr.on("data", (bytes: Buffer) => stderr.push(bytes));
  const [code] = await once(child, "close");
  assert.equal(code, 0, Buffer.concat(stderr).toString());
  return Buffer.concat(stdout).toString();
}

const local = mkdtempSync(join(tmpdir(), "arker-sync-options-"));
writeFileSync(join(local, "main.txt"), "source");
writeFileSync(join(local, ".env"), "dummy-value");
mkdirSync(join(local, "node_modules"));
writeFileSync(join(local, "node_modules", "dummy"), "dependency");
let vm: VM | undefined;
try {
  vm = await new Arker().fork({ source_vm_name: source });
  console.log(`Disposable VM: ${vm.id}`);
  const root = `/tmp/arker-sync-options-${Date.now()}`;
  assert.equal((await vm.run(`mkdir -p ${root}; cd ${root}`)).exitCode, 0);
  const selected = ["--exclude", ".env", "--exclude", "node_modules"];
  const preview = JSON.parse(await cli(["sync-dir", vm.id, local, "project", ...selected, "--dry-run", "--json"]));
  assert.equal(preview.dryRun, true);
  assert.deepEqual(preview.planned.map((entry: { path: string }) => entry.path), ["main.txt"]);
  assert.equal(preview.sent, 0);
  assert.equal((await vm.run(`test ! -e ${root}/project`)).exitCode, 0, "preview created the destination");

  await cli(["sync-dir", vm.id, local, "project", ...selected]);
  assert.equal(new TextDecoder().decode(await vm.sync(`${root}/project/main.txt`)), "source");
  assert.equal((await vm.run(`test ! -e ${root}/project/.env && test ! -e ${root}/project/node_modules`)).exitCode, 0);
  const repeat = JSON.parse(await cli(["sync-dir", vm.id, local, "project", ...selected, "--dry-run", "--json"]));
  assert.deepEqual(repeat.planned, []);
  assert.equal(repeat.skipped, 1);

  const session = await vm.createSession({ cwd: "/tmp" });
  const destination = `arker-explicit-${Date.now()}`;
  await cli(["sync-dir", vm.id, local, destination, "--session-id", session.session_id]);
  assert.equal(new TextDecoder().decode(await vm.sync(`/tmp/${destination}/.env`)), "dummy-value");
  assert.equal(new TextDecoder().decode(await vm.sync(`/tmp/${destination}/node_modules/dummy`)), "dependency");
  mkdirSync(join(local, "empty"));
  symlinkSync("main.txt", join(local, "link"));
  symlinkSync("missing", join(local, "dangling"));
  symlinkSync("/not/a/local/upload/source", join(local, "absolute-link"));
  await cli(["sync-dir", vm.id, local, `${root}/tree`]);
  const kinds = await vm.run(`test -d ${root}/tree/empty && test -L ${root}/tree/link && test -L ${root}/tree/dangling && test -L ${root}/tree/absolute-link && readlink ${root}/tree/link && readlink ${root}/tree/dangling && readlink ${root}/tree/absolute-link`);
  assert.equal(kinds.exitCode, 0, kinds.stderr);
  assert.equal(kinds.stdout, "main.txt\nmissing\n/not/a/local/upload/source\n");
  const emptyLocal = join(local, "only-empty");
  mkdirSync(emptyLocal);
  await cli(["sync-dir", vm.id, emptyLocal, `${root}/empty-root`]);
  assert.equal((await vm.run(`test -d ${root}/empty-root`)).exitCode, 0);
  console.log("PASS cli sync-dir options and entry kinds");
} finally {
  try {
    if (vm) { await vm.delete(); console.log(`Deleted disposable VM: ${vm.id}`); }
  } finally { rmSync(local, { recursive: true, force: true }); }
}
