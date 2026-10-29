import assert from "node:assert/strict";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

async function program(Sandbox: typeof Native.Sandbox, privateTraffic: boolean): Promise<void> {
  let sandbox = await Sandbox.create(privateTraffic ? { network: { allowPublicTraffic: false } } : {});
  const status = async (headers: Record<string, string> = {}) => {
    const response = await fetch(`https://${sandbox.getHost(8080)}/`, { headers, redirect: "error", signal: AbortSignal.timeout(30000) });
    await response.body?.cancel();
    return response.status;
  };
  try {
    const token = sandbox.trafficAccessToken;
    if (privateTraffic) assert.ok(typeof token === "string" && token.length > 0);
    else assert.equal(token, undefined);
    for (const phase of ["created", "reconnect", "full", "filesystem"] as const) {
      if (phase === "full" || phase === "filesystem") assert.equal(await sandbox.pause({ mode: phase }), true);
      if (phase !== "created") {
        sandbox = await Sandbox.connect(sandbox.sandboxId);
        assert.equal(sandbox.trafficAccessToken, token);
      }
      if (phase === "created" || phase === "filesystem") {
        await sandbox.commands.run("python3 -m http.server 8080 --bind 0.0.0.0 >/tmp/traffic-server.log 2>&1", { background: true, timeoutMs: 0 });
        await sandbox.commands.run(`python3 - <<'PY'
import socket,time
deadline=time.monotonic()+20
while True:
    try:
        socket.create_connection(('127.0.0.1',8080),timeout=1).close()
        break
    except OSError:
        assert time.monotonic()<deadline
        time.sleep(.05)
PY`, { timeoutMs: 25000 });
      }
      assert.equal(await status(), privateTraffic ? 403 : 200);
      if (privateTraffic) {
        assert.equal(await status({ "e2b-traffic-access-token": "incorrect" }), 403);
        assert.equal(await status({ "E2B-Traffic-Access-Token": token! }), 200);
        assert.equal(await status({ "X-Access-Token": token! }), 403);
      }
      assert.equal((await sandbox.getInfo()).network?.allowPublicTraffic, !privateTraffic);
    }
  } finally { await sandbox.kill(); }
}

await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"], async ({ setProvider }) => {
  assert.ok(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || process.env.ARKER_SOURCE_VM_ID, "An isolated source is required");
  const nativeKey = process.env.E2B_API_KEY;
  const failures: Error[] = [];
  for (const [provider, module] of [["e2b", Native], ["e2b", Shim], ["arker", Shim]] as const) {
    const route = module === Native ? "reference" : provider === "arker" ? "arker" : "native";
    await setProvider(provider);
    if (provider === "arker") delete process.env.E2B_API_KEY;
    else process.env.E2B_API_KEY = nativeKey;
    for (const privateTraffic of [false, true]) {
      const scenario = `application-traffic-${privateTraffic ? "private" : "public"}`;
      const called = new Set<string>();
      let outcome = "failed";
      try {
        await program(traceVendor(module.Sandbox as unknown as typeof Native.Sandbox, "Sandbox.static", called), privateTraffic);
        outcome = "passed";
      } catch (error) {
        console.error(`${scenario} failed on ${route}: ${error instanceof Error ? error.name : typeof error}`);
        failures.push(new Error(`${scenario}: ${route} failed`));
      } finally { await recordEvidence("e2b", route, scenario, [...called].sort(), outcome); }
    }
  }
  if (failures.length) throw new AggregateError(failures, "Traffic differential failed; inspect evidence rows");
});
