import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { Arker, ArkerError, type VM } from "@arker-ai/sdk";

const source = process.env.ARKER_SOURCE_VM;
assert.ok(source && process.env.ARKER_API_KEY, "set ARKER_API_KEY and ARKER_SOURCE_VM plus placement");
const packageRoot = fileURLToPath(new URL("..", import.meta.url));
let vm: VM | undefined;
try {
  vm = await new Arker().fork({ source_vm_name: source });
  console.log(`Disposable VM: ${vm.id}`);
  for (const scenario of ["interrupt", "force", "inline"] as const) {
    const marker = `/tmp/cli-control-${randomUUID()}`;
    const trap = scenario === "force" ? "printf 'INT\\n'" : "printf 'INT\\n'; exit 23";
    const command = `trap ${JSON.stringify(trap)} INT; printf ready > ${marker}; printf 'READY\\n'; while :; do sleep 1; done`;
    const flags = scenario === "inline" ? ["--memory-mib", "1024", "--json"] : [];
    const child = spawn("node", ["dist/cli.js", "run", vm.id, "--timeout", "30", ...flags, "--", "sh", "-c", command],
      { cwd: packageRoot, stdio: ["ignore", "pipe", "pipe"] });
    const finished = once(child, "close");
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let sent = 0;
    child.stdout.on("data", (bytes: Buffer) => {
      stdout.push(bytes);
      const output = Buffer.concat(stdout).toString();
      if (scenario !== "inline" && sent === 0 && output.includes("READY\n")) {
        sent = 1;
        child.kill("SIGINT");
      }
      if (scenario === "force" && sent === 1 && output.includes("INT\n")) {
        sent = 2;
        child.kill("SIGINT");
      }
    });
    child.stderr.on("data", (bytes: Buffer) => stderr.push(bytes));
    try {
      if (scenario === "inline") {
        const deadline = Date.now() + 20_000;
        for (;;) {
          try {
            assert.equal(new TextDecoder().decode(await vm.sync(marker)), "ready");
            break;
          } catch (error) {
            if (!(error instanceof ArkerError) || error.code !== "not_found" || Date.now() >= deadline) throw error;
            await new Promise((resolve) => setTimeout(resolve, 100));
          }
        }
        sent = 1;
        child.kill("SIGINT");
      }
      const [code] = await finished;
      assert.equal(code, scenario === "force" ? 1 : 23, Buffer.concat(stderr).toString());
      assert.equal(sent, scenario === "force" ? 2 : 1);
      if (scenario === "inline") {
        const result = JSON.parse(Buffer.concat(stdout).toString());
        assert.equal(result.memoryRequestedMib, 1024);
        assert.equal(result.exitCode, 23);
      } else {
        assert.equal(Buffer.concat(stdout).toString(), "READY\nINT\n");
      }
      const after = await vm.run("printf alive");
      assert.equal(after.stdout, "alive");
      console.log(`PASS live CLI ${scenario}`);
    } finally {
      if (child.exitCode === null && child.signalCode === null) { child.kill("SIGKILL"); await finished; }
    }
  }
} finally {
  if (vm) { await vm.delete(); console.log(`Deleted disposable VM: ${vm.id}`); }
}
