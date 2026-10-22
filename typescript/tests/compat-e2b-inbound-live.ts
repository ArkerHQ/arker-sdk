import assert from "node:assert/strict";

import { Sandbox } from "../src/e2b.js";

if (!process.env.E2B_API_KEY) throw new Error("E2B_API_KEY is required");
delete process.env.ARKER_API_KEY;

const sandbox = await Sandbox.create({ timeoutMs: 300_000 });
try {
  const started = await sandbox.commands.run("python3 -m http.server 8080 --bind 0.0.0.0", { background: true });
  assert.equal(started.exitCode, 0);
  const host = (sandbox as Sandbox & { getHost(port: number): string }).getHost(8080);
  assert.match(host, /e2b\.app$/);

  const deadline = Date.now() + 120_000;
  let reached = false;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`https://${host}`, { signal: AbortSignal.timeout(10_000) });
      if (response.ok) {
        reached = true;
        break;
      }
    } catch {
      // The E2B proxy can become ready shortly after the guest listener.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  assert.equal(reached, true, "E2B native ingress did not reach the background daemon");
} finally {
  await sandbox.kill();
}

console.log("PASS E2B native daemon, inbound URL, and delegated getHost");
