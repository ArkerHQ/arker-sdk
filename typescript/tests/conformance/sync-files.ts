import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { Arker, type VM, type CompletedRunResult } from "../../src/index.js";

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const arker = new Arker({
  apiKey: requiredEnv("ARKER_API_KEY"),
  baseUrl: requiredEnv("ARKER_BASE_URL"),
});
let vm: VM | undefined;
try {
  vm = await arker.fork({ source_vm_name: requiredEnv("ARKER_SOURCE_VM") });
  for (const size of [0, 5, 4 * 1024 * 1024 + 1, 20 * 1024 * 1024]) {
    const payload = randomBytes(size);
    const path = `/tmp/sdk-live-${size}.bin`;
    await vm.sync(path, payload);
    const received: Uint8Array = await vm.sync(path);
    assert.equal(received.byteLength, payload.byteLength);
    const digest = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");
    assert.equal(digest(received), digest(payload), `readback differs for ${size} bytes`);
    const guest: CompletedRunResult = await vm.run(`sha256sum ${path} | cut -d' ' -f1`);
    assert.equal(guest.exitCode, 0);
    assert.equal(guest.stdout, `${digest(payload)}\n`);
    assert.equal(guest.stderr, "");
    console.log(`PASS sync file ${size} bytes`);
  }
} finally {
  if (vm) await vm.delete();
}
