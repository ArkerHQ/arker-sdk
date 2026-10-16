import assert from "node:assert/strict";

import { createArkerComputeProvider } from "../src/compat/arker-provider.js";

const apiKey = process.env.ARKER_API_KEY;
if (!apiKey) throw new Error("ARKER_API_KEY is required");

const provider = createArkerComputeProvider({ apiKey, source: process.env.ARKER_SOURCE ?? "ubuntu-coding" });
const sandbox = await provider.sandbox.create({ name: `compat-inbound-${crypto.randomUUID().slice(0, 10)}` });
console.log("created inbound test VM");

async function eventually<T>(probe: () => Promise<T | undefined>, description: string): Promise<T> {
  const deadline = Date.now() + 600_000;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      const value = await probe();
      if (value !== undefined) return value;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`timed out waiting for ${description}`, { cause: lastError });
}

try {
  const serverSource = `
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        pass

HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
`;
  const encodedServer = Buffer.from(serverSource).toString("base64");
  const install = await sandbox.runCommand(`printf %s ${encodedServer} | base64 -d > /tmp/arker-inbound.py`);
  assert.equal(install.exitCode, 0);
  console.log("installed inbound test daemon");
  const finite = await sandbox.startProcess(`printf '%s:%s' "$PWD" "$FINITE_VALUE"; printf finite-err >&2; exit 7`, {
    cwd: "/tmp",
    env: { FINITE_VALUE: "finite-out" },
  });
  const finiteResult = await finite.wait({ timeout: 30_000 });
  assert.equal(finiteResult.exitCode, 7);
  assert.equal(finiteResult.stdout, "/tmp:finite-out");
  assert.equal(finiteResult.stderr, "finite-err");
  const daemon = await sandbox.startProcess("python3 /tmp/arker-inbound.py");

  await eventually(async () => {
    const result = await sandbox.runCommand("curl -fsS http://127.0.0.1:8080/ready");
    return result.exitCode === 0 ? true : undefined;
  }, "guest daemon readiness");
  console.log("guest daemon is ready");

  const reconnected = await provider.sandbox.getById(sandbox.sandboxId);
  assert.ok(reconnected, "sandbox must reconnect while its daemon is running");
  const url = await reconnected.getUrl({ port: 8080 });
  const response = await eventually(async () => {
    const candidate = await fetch(`${url}/probe?source=compat`, {
      signal: AbortSignal.timeout(10_000),
      headers: {
        authorization: `Bearer ${apiKey}`,
        "x-arker-user-id": "forged",
      },
    });
    return candidate.ok ? candidate : undefined;
  }, "authenticated Arker ingress");
  console.log("authenticated ingress reached the daemon");
  const received = await response.json() as { path: string; headers: Record<string, string> };
  assert.equal(received.path, "/probe?source=compat");
  assert.equal(received.headers.authorization, undefined, "Arker credentials must not reach the guest daemon");
  assert.equal(received.headers["x-arker-user-id"], undefined, "client-supplied internal identity must be stripped");

  const daemonState = await daemon.status();
  assert.equal(daemonState.status, "running");
  await daemon.kill();
  await eventually(async () => (await daemon.status()).status === "exited" ? true : undefined, "daemon termination");
} finally {
  await sandbox.destroy();
}

console.log("PASS compat daemon and authenticated inbound interception");
