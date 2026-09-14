/**
 * Behavioral conformance test for VM.syncDir (ARK-268).
 *
 * Exercises the SDK's rsync-style directory sync end-to-end against a live VM:
 *   fork -> full sync -> repeat (delta=0) -> edit one file (delta=1) ->
 *   add a file (delta=1) -> byte-exact readback (incl. nested + >4 MB presigned)
 *
 * Unlike fork-run-sync.ts (raw HTTP, contract drift), this drives the actual
 * SDK method, since syncDir orchestrates manifest-diff + tarball + guest extract
 * client-side. The remote manifest is authoritative, so the delta assertions
 * (repeat sends nothing; an edit sends exactly one) are the real contract.
 *
 *   ARKER_API_KEY=ark_... ARKER_BASE_URL=http://host:8080/api \
 *   ARKER_SOURCE_VM=<source-name> bun tests/conformance/sync-dir.ts
 */

import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { Arker, type SyncDirResult, type VM } from "../../src/index.js";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value?.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

function eq(actual: SyncDirResult, sent: number, skipped: number, ctx: string): void {
  assert(
    actual.sent === sent && actual.skipped === skipped,
    `${ctx}: expected sent=${sent} skipped=${skipped}, got sent=${actual.sent} skipped=${actual.skipped}`,
  );
}

function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

async function main(): Promise<void> {
  const arker = new Arker({
    apiKey: requiredEnv("ARKER_API_KEY"),
    baseUrl: requiredEnv("ARKER_BASE_URL"),
  });
  const source = requiredEnv("ARKER_SOURCE_VM");

  // A nested local tree: two small files, one nested, and one >CHUNK_SIZE (4 MB)
  // file so the tarball takes the presigned path rather than inline.
  const local = mkdtempSync(join(tmpdir(), "arker-syncdir-"));
  const bigBytes = randomBytes(5 * 1024 * 1024); // >4 MB -> presigned tarball
  const aBytes = new TextEncoder().encode("alpha\n");
  const nestedBytes = new TextEncoder().encode("nested-original\n");
  mkdirSync(join(local, "nested"), { recursive: true });
  writeFileSync(join(local, "a.txt"), aBytes);
  writeFileSync(join(local, "nested", "b.txt"), nestedBytes);
  writeFileSync(join(local, "big.bin"), bigBytes);

  const remote = `/home/user/syncdir-${Date.now()}`;
  let vm: VM | undefined;
  try {
    vm = await arker.fork({ source_vm_name: source });

    // 1) Full sync: all 3 files are new.
    eq(await vm.syncDir(local, remote), 3, 0, "full sync");

    // 2) Byte-exact readback: nested small file + the large presigned file.
    const gotNested = await vm.sync(`${remote}/nested/b.txt`);
    assert(bytesEqual(gotNested, nestedBytes), "nested readback mismatch");
    const gotBig = await vm.sync(`${remote}/big.bin`);
    assert(bytesEqual(gotBig, bigBytes), "big-file readback mismatch (presigned tarball)");

    // 3) Repeat with no local change: the manifest diff must send NOTHING.
    eq(await vm.syncDir(local, remote), 0, 3, "repeat (delta) sync");

    // 4) Edit one file: exactly one file is re-sent.
    const editedBytes = new TextEncoder().encode("nested-EDITED\n");
    writeFileSync(join(local, "nested", "b.txt"), editedBytes);
    eq(await vm.syncDir(local, remote), 1, 2, "edit sync");
    const gotEdited = await vm.sync(`${remote}/nested/b.txt`);
    assert(bytesEqual(gotEdited, editedBytes), "edited readback mismatch");

    // 5) Add a new file: exactly one file is sent.
    writeFileSync(join(local, "c.txt"), new TextEncoder().encode("charlie\n"));
    eq(await vm.syncDir(local, remote), 1, 3, "add-file sync");

    // 6) The accelerator cache is a pure optimization — same delta result.
    const cache = new Map();
    eq(await vm.syncDir(local, remote, { cache }), 0, 4, "cached repeat sync");

    console.log("PASS sync-dir");
  } finally {
    if (vm) {
      try { await vm.delete(); } catch (error) { console.warn(`WARN: delete ${vm.id}: ${String(error)}`); }
    }
    rmSync(local, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`FAIL sync-dir: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
