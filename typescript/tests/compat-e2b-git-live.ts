import assert from "node:assert/strict";
import { Sandbox as Native } from "e2b";
import { Sandbox as Shim } from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";
import { withLiveRouter } from "./compat-live-router.js";

type SandboxConstructor = {
  create(): Promise<Native | Shim>;
};

async function program(Sandbox: SandboxConstructor): Promise<void> {
  const sandbox = await Sandbox.create();
  const root = "/tmp/compat-e2b-git";
  const remote = `${root}/remote.git`;
  const checkout = `${root}/checkout`;
  try {
    await sandbox.commands.run(`rm -rf ${root}`);
    await sandbox.git.init(remote, { bare: true, initialBranch: "main" });
    await sandbox.git.clone(remote, { path: checkout });
    await sandbox.git.configureUser("Compatibility Test", "compat@arker.invalid", {
      scope: "local",
      path: checkout,
    });
    await sandbox.files.write(`${checkout}/hello.txt`, "hello\n");
    await sandbox.git.add(checkout, { files: ["hello.txt"] });
    await sandbox.git.commit(checkout, "initial");
    assert.equal((await sandbox.git.status(checkout)).isClean, true);
    await sandbox.git.createBranch(checkout, "feature");
    const branches = await sandbox.git.branches(checkout);
    assert.equal(branches.currentBranch, "feature");
    assert.deepEqual(new Set(branches.branches), new Set(["feature", "main"]));
    await sandbox.git.checkoutBranch(checkout, "main");
    assert.equal((await sandbox.git.status(checkout)).currentBranch, "main");
  } finally {
    await sandbox.kill();
  }
}

await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
  const nativeKey = process.env.E2B_API_KEY;
  for (const [provider, constructor] of [
    ["e2b", Native],
    ["e2b", Shim],
    ["arker", Shim],
  ] as const) {
    await setProvider(provider);
    if (provider === "arker") delete process.env.E2B_API_KEY;
    else process.env.E2B_API_KEY = nativeKey;
    const called = new Set<string>();
    let outcome = "failed";
    try {
      await program(traceVendor(constructor, "Sandbox.static", called));
      outcome = "passed";
    } finally {
      await recordEvidence(
        "e2b",
        constructor === Native ? "reference" : provider === "arker" ? "arker" : "native",
        "import-only-local-git-workflow",
        [...called].sort(),
        outcome,
      );
    }
  }
});
