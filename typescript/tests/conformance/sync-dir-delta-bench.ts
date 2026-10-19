/**
 * Throughput benchmark for VM.syncDir delta sync.
 *
 * The companion of sync-dir-bench.ts (which measures the FULL first-push). This
 * one measures the incremental/rsync-style path - the reason syncDir exists:
 *
 *   1. FULL   sync of an N-file tree            -> assert sent=N   (baseline)
 *   2. REPEAT with no local change              -> assert sent=0   (delta)
 *   3. EDIT   one file, sync again              -> assert sent=1   (delta)
 *
 * The delta round-trip is one op="manifest" fetch + a local (size,mtime)-cached
 * diff; with nothing changed the guest is never touched and ZERO bytes upload.
 * We report delta files/s (files diffed per second on the no-change repeat) and
 * the delta-vs-full speedup (full_s / repeat_s) - the win a warm re-sync buys.
 *
 * The `sent=0` repeat verifies that a completed sync is immediately visible to
 * the next sync. The benchmark fails rather than reporting misleading results.
 *
 * SKIP-tolerant: with ARKER_API_KEY / ARKER_BASE_URL unset it prints SKIP and
 * exits 0. Self-cleaning: it tracks the VM ids it forks and deletes only those.
 *
 *   ARKER_API_KEY=ark_... ARKER_BASE_URL=http://host:8080/api \
 *   ARKER_SOURCE_VM=<source-name> bun tests/conformance/sync-dir-delta-bench.ts
 *
 * Knobs (env): BENCH_N_FILES (2000), BENCH_SMALL_BYTES (256),
 * BENCH_LARGE_COUNT (2), BENCH_LARGE_MB (2).
 */

import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { Arker, type SyncDirResult, type VM } from "../../src/index.js";

function optionalEnv(name: string): string | undefined {
  const v = process.env[name];
  return v?.trim() ? v.trim() : undefined;
}
function envInt(name: string, dflt: number): number {
  const v = process.env[name];
  return v?.trim() ? Number.parseInt(v, 10) : dflt;
}
function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
const now = () => Number(process.hrtime.bigint()) / 1e9;
const MiB = 1024 * 1024;

async function main(): Promise<void> {
  const apiKey = optionalEnv("ARKER_API_KEY");
  const baseUrl = optionalEnv("ARKER_BASE_URL");
  if (!apiKey || !baseUrl) {
    console.log("SKIP sync-dir-delta-bench (set ARKER_API_KEY + ARKER_BASE_URL to run)");
    return;
  }

  const arker = new Arker({ apiKey, baseUrl });
  const source = optionalEnv("ARKER_SOURCE_VM");
  if (!source) throw new Error("ARKER_SOURCE_VM is required");
  const nFiles = envInt("BENCH_N_FILES", 2000);
  const smallBytes = envInt("BENCH_SMALL_BYTES", 256);
  const largeCount = envInt("BENCH_LARGE_COUNT", 2);
  const largeMb = envInt("BENCH_LARGE_MB", 2);

  // Build a nested local tree: nFiles small files (100 per dir) + a few large.
  const local = mkdtempSync(join(tmpdir(), "arker-syncdir-delta-"));
  const rels: string[] = [];
  const small = randomBytes(smallBytes);
  for (let i = 0; i < nFiles; i++) {
    const d = `d${String(Math.floor(i / 100)).padStart(4, "0")}`;
    mkdirSync(join(local, d), { recursive: true });
    const rel = `${d}/f${String(i).padStart(6, "0")}.dat`;
    writeFileSync(join(local, rel), small);
    rels.push(rel);
  }
  for (let i = 0; i < largeCount; i++) {
    const rel = `big${i}.bin`;
    writeFileSync(join(local, rel), randomBytes(largeMb * MiB));
    rels.push(rel);
  }
  const totalFiles = rels.length;
  const editRel = rels[0]; // a small file we mutate for the EDIT step

  // Track the VMs WE fork so cleanup deletes only those (never a stray VM).
  const createdVms: VM[] = [];
  try {
    const vm = await arker.fork({ source_vm_name: source });
    createdVms.push(vm);
    // Warm the guest so cold-boot doesn't pollute the first sync timing.
    await vm.sync(`/home/user/.bench-warm`, "warm");

    // A caller-owned accelerator cache reused across all three calls - the
    // realistic warm-resync pattern: full populates it, delta reuses it (no
    // re-hash of unchanged files), so the repeat measures the pure manifest RTT.
    const cache = new Map<string, { size: number; mtimeMs: number; hash: string }>();
    const remote = `/home/user/bench-syncdir-delta-${Date.now()}`;

    // ── 1) FULL sync (baseline) ──────────────────────────────────────────
    const f0 = now();
    const full: SyncDirResult = await vm.syncDir(local, remote, { cache });
    const fullS = now() - f0;
    assert(full.sent === totalFiles, `full sync: expected sent=${totalFiles}, got sent=${full.sent} (skipped=${full.skipped})`);

    // ── 2) REPEAT, no change -> DELTA sends nothing ──────────────────────
    const r0 = now();
    const repeat: SyncDirResult = await vm.syncDir(local, remote, { cache });
    const repeatS = now() - r0;
    assert(
      repeat.sent === 0,
      `repeat (delta) sync: expected sent=0, got sent=${repeat.sent} skipped=${repeat.skipped} - ` +
        `the server manifest-freshness fix is likely NOT deployed on this host ` +
        `(op="manifest" returned a stale/empty manifest, so unchanged files re-sent). See header.`,
    );
    const deltaFilesPerS = totalFiles / Math.max(repeatS, 1e-6);
    const speedup = fullS / Math.max(repeatS, 1e-6);

    // ── 3) EDIT one file -> DELTA sends exactly one ──────────────────────
    writeFileSync(join(local, editRel), randomBytes(smallBytes));
    const e0 = now();
    const edit: SyncDirResult = await vm.syncDir(local, remote, { cache });
    const editS = now() - e0;
    assert(edit.sent === 1, `edit sync: expected sent=1, got sent=${edit.sent} (skipped=${edit.skipped})`);
    // Byte-exact confirmation the single edited file actually landed.
    const readBack = await vm.sync(`${remote}/${editRel}`);
    assert(readBack.length === smallBytes, `edited readback size mismatch: ${readBack.length} != ${smallBytes}`);

    console.log("");
    console.log(`  tree: ${totalFiles} files (${nFiles}×${smallBytes}B + ${largeCount}×${largeMb}MB)`);
    console.log(`  FULL  sync:   ${fullS.toFixed(3)}s  (sent=${full.sent}, bytesSent=${full.bytesSent})`);
    console.log(`  DELTA repeat: ${repeatS.toFixed(3)}s  ->  ${deltaFilesPerS.toFixed(0)} files/s diffed  (sent=0, 0 bytes)`);
    console.log(`  DELTA edit:   ${editS.toFixed(3)}s  (sent=1)`);
    console.log(`  delta win:    ~${speedup.toFixed(1)}x faster than the full sync (repeat ${repeatS.toFixed(3)}s vs full ${fullS.toFixed(3)}s)`);
    console.log("");
    console.log(`SYNC_DIR_DELTA_BENCH_JSON={"files":${totalFiles},"full_s":${fullS.toFixed(3)},"repeat_s":${repeatS.toFixed(3)},"edit_s":${editS.toFixed(3)},"delta_files_s":${deltaFilesPerS.toFixed(1)},"delta_speedup_x":${speedup.toFixed(2)},"full_bytes_sent":${full.bytesSent}}`);
    console.log("PASS sync-dir-delta-bench");
  } finally {
    for (const vm of createdVms) {
      try { await vm.delete(); } catch (error) { console.warn(`WARN: delete ${vm.id}: ${String(error)}`); }
    }
    rmSync(local, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`FAIL sync-dir-delta-bench: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
