/**
 * Adversarial conformance test for VM.syncDir (ARK-268 / manifest-freeze).
 *
 * Drives the real SDK syncDir with BIG and SMALL payloads while the VM runs a
 * concurrent network+compute+write workload, and asserts the running-VM
 * manifest-freeze contract end-to-end: NEITHER the sync NOR the run breaks.
 *   * SMALL + BIG full sync push every file (BIG > CHUNK_SIZE → presigned tarball).
 *   * The immediate DELTA re-sync sends NOTHING (sent=0) — the fix: op:manifest
 *     FIFREEZE-quiesces the eager-paused guest so the host sees the tar-x writes.
 *   * Byte-exact readback of a nested small file and the >4 MB big file.
 *   * The concurrent workload completes cleanly (exit 0) — the FIFREEZE stalls
 *     it briefly but must not break it.
 *
 * Unlike sync-dir.ts (quiescent VM), this fires syncDir against a VM that is
 * actively busy, which is the real-world case that regressed the delta path.
 *
 *   ARKER_API_KEY=ark_... ARKER_BASE_URL=http://host:8080/api \
 *   ARKER_SOURCE_VM=<source-name> bun tests/conformance/sync-dir-adversarial.ts
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
function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
const secs = () => Number(process.hrtime.bigint()) / 1e9;

async function main(): Promise<void> {
  const arker = new Arker({
    apiKey: requiredEnv("ARKER_API_KEY"),
    baseUrl: requiredEnv("ARKER_BASE_URL"),
  });
  const source = requiredEnv("ARKER_SOURCE_VM");
  const SMALL_N = 600;
  const BIG_N = 2;

  // SMALL: 600 tiny files (incl. one nested). BIG: 2 x 6 MB (> CHUNK_SIZE 4 MB
  // so the tarball takes the presigned path, not inline).
  const small = mkdtempSync(join(tmpdir(), "arker-syncdir-adv-small-"));
  const nestedBytes = new TextEncoder().encode("nested-small\n");
  mkdirSync(join(small, "nest"), { recursive: true });
  writeFileSync(join(small, "nest", "b.txt"), nestedBytes);
  for (let i = 0; i < SMALL_N - 1; i++) {
    writeFileSync(join(small, `f${i}.txt`), new TextEncoder().encode(`s${i}\n`));
  }
  const big = mkdtempSync(join(tmpdir(), "arker-syncdir-adv-big-"));
  const bigBytes = randomBytes(6 * 1024 * 1024);
  writeFileSync(join(big, "blob0.bin"), bigBytes);
  writeFileSync(join(big, "blob1.bin"), randomBytes(6 * 1024 * 1024));

  const smallRemote = `/home/user/adv-small-${Date.now()}`;
  const bigRemote = `/home/user/adv-big-${Date.now()}`;
  let vm: VM | undefined;
  try {
    vm = await arker.fork({ source_vm_name: source });
    await vm.run("echo warm");

    // Concurrent workload: network + compute + writes, continuously ~22 s.
    const workloadSecs = Number(process.env.ADV_WORKLOAD_SECS ?? "22");
    const netUrl = process.env.ADV_NET_URL ?? "https://example.com";
    // /tmp/wl (not /root) so this workload's scratch dir is writable
    // regardless of which account the VM executes commands as, and stays
    // clearly outside the smallRemote/bigRemote sync targets above.
    const workload =
      `mkdir -p /tmp/wl; n=0; i=0; end=$((SECONDS+${workloadSecs})); ` +
      `while [ $SECONDS -lt $end ]; do i=$((i+1)); echo line$i >> /tmp/wl/log.txt; ` +
      `seq 1 50000 | sha256sum >/dev/null; ` +
      `c=$(curl -s -m5 -o /dev/null -w "%{http_code}" ${netUrl} 2>/dev/null); ` +
      `[ "$c" = 200 ] && n=$((n+1)); done; echo WL_DONE lines=$i net200=$n`;
    const bg = await vm.run(workload, { time_to_background: 0 });
    console.log(`concurrent workload runId=${bg.runId} (network+compute+write, ~${workloadSecs}s)`);

    const timed = async (label: string, fn: () => Promise<SyncDirResult>): Promise<SyncDirResult> => {
      const t0 = secs();
      const r = await fn();
      console.log(`  [${label}] ${(secs() - t0).toFixed(3)}s sent=${r.sent} skipped=${r.skipped} bytesSent=${r.bytesSent}`);
      return r;
    };

    // SMALL: full then delta.
    const s1 = await timed("small full ", () => vm!.syncDir(small, smallRemote));
    assert(s1.sent === SMALL_N, `small full: sent=${s1.sent} expected ${SMALL_N}`);
    const s2 = await timed("small DELTA", () => vm!.syncDir(small, smallRemote));
    assert(s2.sent === 0 && s2.skipped === SMALL_N, `small delta: sent=${s2.sent} skipped=${s2.skipped} expected 0/${SMALL_N}`);

    // BIG (presigned tarball): full then delta.
    const b1 = await timed("big   full ", () => vm!.syncDir(big, bigRemote));
    assert(b1.sent === BIG_N, `big full: sent=${b1.sent} expected ${BIG_N} (presigned tarball)`);
    const b2 = await timed("big   DELTA", () => vm!.syncDir(big, bigRemote));
    assert(b2.sent === 0 && b2.skipped === BIG_N, `big delta: sent=${b2.sent} skipped=${b2.skipped} expected 0/${BIG_N}`);

    // Byte-exact readback (nested small + >4 MB presigned big).
    assert(bytesEqual(await vm.sync(`${smallRemote}/nest/b.txt`), nestedBytes), "nested small readback mismatch");
    assert(bytesEqual(await vm.sync(`${bigRemote}/blob0.bin`), bigBytes), "6 MB big readback mismatch (presigned)");

    // The concurrent workload must have completed cleanly — the FIFREEZEs
    // stalled it but must not have broken it.
    let run = await vm.getRun(bg.runId);
    for (let i = 0; i < 90 && run.state !== "completed" && run.state !== "failed"; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      run = await vm.getRun(bg.runId);
    }
    const out = (run.stdout ?? "").trim();
    console.log(`  workload: state=${run.state} exit=${run.exit_code} tail=[${out.slice(-60)}]`);
    assert(run.state === "completed" && run.exit_code === 0, `workload did not complete cleanly: state=${run.state} exit=${run.exit_code}`);
    assert(/WL_DONE/.test(out), "workload did not reach WL_DONE (writes+compute broke under concurrent FIFREEZE)");

    console.log("PASS sync-dir-adversarial");
  } finally {
    if (vm) {
      try { await vm.delete(); } catch (error) { console.warn(`WARN: delete ${vm.id}: ${String(error)}`); }
    }
    rmSync(small, { recursive: true, force: true });
    rmSync(big, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`FAIL sync-dir-adversarial: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
