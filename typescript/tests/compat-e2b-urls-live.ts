import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

// Keep scenario semantics aligned with the Python URL differential program.
export const cases = ["round-trip", "multi-file", "custom-user", "range", "cancel-download", "cancel-upload", "wrong-path", "wrong-user", "wrong-expiry", "expiry"] as const;
type Case = typeof cases[number];
const digest = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");
const request = (url: string | URL, options?: RequestInit) => fetch(url, {
  redirect: "error", signal: AbortSignal.timeout(60000), ...options,
});
async function expectBytes(response: Response, status: number, expected: Uint8Array): Promise<void> {
  assert.equal(response.status, status);
  const actual = new Uint8Array(await response.arrayBuffer());
  assert.equal(actual.length, expected.length);
  assert.equal(digest(actual), digest(expected));
}
function multipart(files: Array<[string, Uint8Array]>): FormData {
  const form = new FormData();
  for (const [name, bytes] of files) form.append("file", new Blob([new Uint8Array(bytes)]), name);
  return form;
}

export async function program(Sandbox: typeof Native.Sandbox, scenario: Case, measurements: number[]): Promise<void> {
  const sandbox = await Sandbox.create();
  try {
    const prefix = `/tmp/url-${randomUUID()}`;
    const path = `${prefix}-file`;
    const payload = Uint8Array.from({ length: 8 * 1024 * 1024 }, (_, index) => index % 256);
    const mint = async (url: () => Promise<string>) => {
      const started = performance.now();
      const value = await url();
      measurements.push((performance.now() - started) / 1000);
      assert.equal(typeof value, "string");
      return value;
    };
    const download = (options?: Parameters<Native.Sandbox["downloadUrl"]>[1]) => mint(() => sandbox.downloadUrl(path, options));
    const upload = (target = path, options?: Parameters<Native.Sandbox["downloadUrl"]>[1]) => mint(() => sandbox.uploadUrl(target, options));
    if (scenario === "multi-file") {
      const files: Array<[string, Uint8Array]> = [[`${prefix}-a`, new TextEncoder().encode("first")], [`${prefix}-b`, new TextEncoder().encode("second")]];
      const response = await request(await upload(""), { method: "POST", body: multipart(files) });
      assert.equal(response.status, 200);
      assert.deepEqual((await response.json() as Array<{ path: string }>).map(entry => entry.path), files.map(([name]) => name));
      for (const [name, bytes] of files) assert.equal(digest(await sandbox.files.read(name, { format: "bytes" })), digest(bytes));
    } else if (scenario === "custom-user") {
      const user = `url_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
      const home = `/home/${user}`;
      await sandbox.commands.run(`useradd -m -d ${home} ${user}`, { user: "root" });
      const response = await request(await upload("~/owned", { user }), { method: "POST", body: multipart([["ignored", new TextEncoder().encode("owned")]]) });
      assert.equal(response.status, 200);
      await response.arrayBuffer();
      assert.equal((await sandbox.files.getInfo(`${home}/owned`)).owner, user);
      await sandbox.commands.run(`chmod 000 ${home}/owned`, { user: "root" });
      await expectBytes(await request(await mint(() => sandbox.downloadUrl(`${home}/owned`, { user: "user" }))), 200, new TextEncoder().encode("owned"));
    } else if (scenario === "cancel-upload") {
      const prefixBytes = payload.slice(0, 1024 * 1024);
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        controller.enqueue(new TextEncoder().encode('--upload-boundary\r\nContent-Disposition: form-data; name="file"; filename="ignored"\r\n\r\n'));
        controller.enqueue(prefixBytes);
        // Keep the body open until the caller observes persisted bytes and aborts.
      } });
      const url = await upload();
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 60000);
      let settled = false;
      const uploading = request(url, { method: "POST", body, signal: controller.signal,
        headers: { "Content-Type": "multipart/form-data; boundary=upload-boundary" }, duplex: "half" } as RequestInit & { duplex: "half" })
        .then(response => { settled = true; return { response }; }, error => { settled = true; return { error }; });
      try {
        const deadline = performance.now() + 30000;
        while (true) {
          assert.equal(settled, false, "Upload ended before cancellation");
          if (await sandbox.files.exists(path) && (await sandbox.files.getInfo(path)).size >= prefixBytes.length / 2) break;
          assert.ok(performance.now() < deadline, "Upload must persist bytes before body completion");
          await new Promise(resolve => setTimeout(resolve, 10));
        }
      } finally {
        controller.abort();
        clearTimeout(timer);
        const outcome = await uploading;
        if ("response" in outcome) await outcome.response.body?.cancel();
      }
      const partial = await sandbox.files.read(path, { format: "bytes" });
      assert.ok(partial.length >= prefixBytes.length / 2 && partial.length <= prefixBytes.length);
      assert.equal(digest(partial), digest(prefixBytes.slice(0, partial.length)));
    } else if (scenario === "round-trip") {
      const response = await request(await upload(), { method: "POST", body: multipart([["ignored", payload]]) });
      assert.equal(response.status, 200);
      assert.equal((await response.json() as Array<{ path: string }>)[0].path, path);
      await expectBytes(await request(await download()), 200, payload);
    } else {
      await sandbox.files.write(path, payload.buffer);
      const url = await download(scenario === "expiry" ? { useSignatureExpiration: 0 } : undefined);
      if (scenario === "expiry") {
        const deadline = performance.now() + 5000;
        while (true) {
          const response = await request(url);
          await response.body?.cancel();
          if (response.status === 401) break;
          assert.equal(response.status, 200);
          assert.ok(performance.now() < deadline, "Zero-second signature outlived its Unix second");
        }
        await expectBytes(await request(await download()), 200, payload);
      } else if (scenario === "range") {
        const response = await request(url, { headers: { Range: "bytes=100-199", "Accept-Encoding": "identity" } });
        assert.equal(response.headers.get("content-range"), `bytes 100-199/${payload.length}`);
        await expectBytes(response, 206, payload.slice(100, 200));
      } else if (scenario === "cancel-download") {
        const response = await request(url, { headers: { "Accept-Encoding": "identity" } });
        assert.equal(response.status, 200);
        const reader = response.body!.getReader();
        try {
          const { value, done } = await reader.read();
          assert.ok(!done && value.length > 0 && value.length < payload.length);
          assert.equal(digest(value), digest(payload.slice(0, value.length)));
        } finally { await reader.cancel(); reader.releaseLock(); }
        await expectBytes(await request(url), 200, payload);
      } else {
        const changes = { "wrong-path": ["path", `${prefix}-other`], "wrong-user": ["username", "root"], "wrong-expiry": ["signature_expiration", "4294967295"] } as const;
        const [name, value] = changes[scenario];
        const changed = new URL(url); changed.searchParams.set(name, value);
        const response = await request(changed);
        assert.equal(response.status, 401);
        await response.body?.cancel();
        await expectBytes(await request(url), 200, payload);
      }
    }
  } finally { assert.equal(await sandbox.kill(), true); }
}

async function main(): Promise<void> {
  const selected = process.env.COMPAT_E2B_CASES?.split(",") ?? [...cases];
  for (const name of selected) assert.ok(cases.includes(name as Case), `Unknown URL case: ${name}`);
  const routes = process.env.COMPAT_E2B_ROUTES?.split(",") ?? ["reference", "native", "arker"];
  for (const route of routes) assert.ok(["reference", "native", "arker"].includes(route));
  await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"], async ({ setProvider }) => {
    assert.ok(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || process.env.ARKER_SOURCE_VM_ID, "An isolated source is required");
    const nativeKey = process.env.E2B_API_KEY;
    const failures: Error[] = [];
    for (const [provider, module] of [["e2b", Native], ["e2b", Shim], ["arker", Shim]] as const) {
      const route = module === Native ? "reference" : provider === "arker" ? "arker" : "native";
      if (!routes.includes(route)) continue;
      await setProvider(provider);
      if (provider === "arker") delete process.env.E2B_API_KEY;
      else process.env.E2B_API_KEY = nativeKey;
      for (const scenario of selected as Case[]) {
        const called = new Set<string>();
        const measurements: number[] = [];
        let outcome = "failed";
        try {
          await program(traceVendor(module.Sandbox as unknown as typeof Native.Sandbox, "Sandbox.static", called), scenario, measurements);
          outcome = "passed";
        } catch (error) {
          console.error(`URL ${scenario} failed on ${route}: ${error instanceof Error ? error.name : typeof error}`);
          failures.push(new Error(`URL ${scenario}: ${route} failed`));
        } finally {
          console.log(JSON.stringify({ route, scenario, urlMintSeconds: measurements }));
          await recordEvidence("e2b", route, `import-only-url-${scenario}`, [...called].sort(), outcome);
        }
      }
    }
    if (failures.length) throw new AggregateError(failures, "URL differential failed; inspect evidence rows");
  });
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
