import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { Arker, type VM } from "@arker-ai/sdk";

const source = process.env.ARKER_SOURCE_VM;
assert.ok(source && process.env.ARKER_API_KEY, "set ARKER_API_KEY and ARKER_SOURCE_VM plus placement");
const packageRoot = fileURLToPath(new URL("..", import.meta.url));
let vm: VM | undefined;
try {
  vm = await new Arker().fork({ source_vm_name: source });
  console.log(`Disposable VM: ${vm.id}`);
  const child = spawn("node", ["dist/cli.js", "run", vm.id, "--timeout", "30", "--", "sh", "-c",
    "printf 'start\\000'; cat; printf 'warning\\n' >&2; sleep 4; printf 'done\\n'; exit 7"],
  { cwd: packageRoot, stdio: ["pipe", "pipe", "pipe"] });
  child.stdin.end(Buffer.from([0, 255, 128, 10]));
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let firstOutputAt: number | undefined;
  child.stdout.on("data", (bytes: Buffer) => {
    firstOutputAt ??= Date.now();
    stdout.push(bytes);
  });
  child.stderr.on("data", (bytes: Buffer) => stderr.push(bytes));
  const [code] = await once(child, "close");
  const finishedAt = Date.now();
  assert.equal(code, 7, Buffer.concat(stderr).toString());
  assert.deepEqual(Buffer.concat(stdout), Buffer.concat([Buffer.from("start\0"), Buffer.from([0, 255, 128, 10]), Buffer.from("done\n")]));
  assert.equal(Buffer.concat(stderr).toString(), "warning\n");
  assert.ok(firstOutputAt !== undefined && finishedAt - firstOutputAt >= 1_000,
    "output was not visible before the command finished");
  console.log("PASS cli live stdin bytes, EOF and output before completion");
} finally {
  if (vm) { await vm.delete(); console.log(`Deleted disposable VM: ${vm.id}`); }
}
