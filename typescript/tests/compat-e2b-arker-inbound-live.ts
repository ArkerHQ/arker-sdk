import assert from "node:assert/strict";

import { Sandbox } from "../src/e2b.js";

const apiKey = process.env.ARKER_API_KEY;
if (!apiKey) throw new Error("ARKER_API_KEY is required");

const sandbox = await Sandbox.create();
try {
  assert.equal(sandbox.provider, "arker");
  const started = await sandbox.commands.run("python3 -m http.server 8080 --bind 127.0.0.1", { background: true });
  assert.equal(started.exitCode, 0);

  const deadline = Date.now() + 120_000;
  let reached = false;
  const host = sandbox.getHost(8080);
  while (Date.now() < deadline) {
    try {
      const local = await sandbox.commands.run("curl -fsS --max-time 1 http://127.0.0.1:8080/");
      if (local.exitCode !== 0) throw new Error("guest listener is not ready");
      const response = await fetch(`https://${host}`, {
        signal: AbortSignal.timeout(10_000),
        headers: { authorization: `Bearer ${apiKey}` },
      });
      if (response.ok) {
        reached = true;
        break;
      }
    } catch {
      // Readiness is established by the guest listener and public relay together.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  assert.equal(reached, true, "E2B-compatible Arker ingress did not reach the daemon");
} finally {
  await sandbox.kill();
}

console.log("PASS E2B-compatible Arker daemon and synchronous getHost");
