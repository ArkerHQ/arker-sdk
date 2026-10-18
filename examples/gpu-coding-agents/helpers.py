"""Plumbing for launch.py: the client, retries, VM bookkeeping and the report.

Nothing here is worth reading to learn the SDK - the calls that matter
(`ar.fork`, `vm.run`, `vm.delete`) are in launch.py. This file is the
scaffolding that keeps a demo honest: retrying a delete so a run cannot leak a
GPU, and counting what happened.
"""

import argparse
import collections
import json
import os
import threading
import time

from arker import Arker, ArkerError, RetryOptions

import base_spec as spec

# GPU platforms live here. ARKER_BASE_URL, if set, overrides the placement.
PROVIDER = os.environ.get("ARKER_PROVIDER", "arker")
REGION = os.environ.get("ARKER_REGION", "us-west")

# How long a foreground run may block before the API hands it back.
SYNC_WINDOW_SECS = 80

EVENTS_FILE = os.environ.get("FLEET_EVENTS", "fleet_events.jsonl")


def client():
    """The SDK client the whole demo shares.

    The SDK already retries `unavailable`/`stale_route`/`capacity_unavailable`
    and honours the server's Retry-After; this just gives it a longer leash,
    since eight threads forking and deleting VMs meet all three routinely.
    """
    return Arker(provider=PROVIDER, region=REGION,
                 retry=RetryOptions(attempts=6, base_delay_s=1.0, max_delay_s=15.0))


# ── running commands ────────────────────────────────────────────────────────


def stdout_of(result):
    """A run's stdout. Empty for a run that backgrounded rather than completing."""
    return getattr(result, "stdout", None) or ""


def slice_mib(vm, default=None):
    """VRAM the platform resolved `vgpu` to, read off a forked VM."""
    res = getattr(vm, "resources", None)
    got = getattr(res, "gpu_vram_mib", None) if res is not None else None
    try:
        return int(got) if got else default
    except (TypeError, ValueError):
        return default


def run(vm, command, session_idx=1, timeout=600, attempts=4):
    """vm.run, retried through the one failure the SDK cannot see for itself.

    An idle VM can suspend between runs, and a run landing mid-suspend comes back
    `exit 128 ... in state stopped` - a successful HTTP call reporting a failed
    exec, so no HTTP-level retry applies. The next run restores the VM.
    """
    last = None
    for i in range(attempts):
        try:
            r = vm.run(command, session_idx=session_idx, timeout=timeout,
                       time_to_background=SYNC_WINDOW_SECS)
        except ArkerError as e:
            last = e
            time.sleep(5 + 5 * i)
            continue
        if not (getattr(r, "exit_code", None) == 128
                and "in state stopped" in (getattr(r, "stderr", None) or "")):
            return r
        last = r
        time.sleep(10 + 10 * i)
    if isinstance(last, ArkerError):
        raise last
    return last


def wait_marker(vm, tag, timeout_s, label, poll_s=20):
    """Poll a detached job's marker file. Returns (rc, elapsed); rc is None on timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(poll_s)
        try:
            r = run(vm, spec.poll_cmd(tag), session_idx=4, timeout=60)
        except ArkerError as e:
            print(f"   {label}: poll error {e.status}")
            continue
        out = stdout_of(r).strip()
        if out and "RUNNING" not in out:
            return out.split()[0], int(time.time() - t0)
        el = int(time.time() - t0)
        if el % 120 < poll_s:
            print(f"   {label}: {el}s elapsed…", flush=True)
    return None, int(time.time() - t0)


# ── deleting, and making sure it happened ───────────────────────────────────

# Every VM this run forked and has not confirmed deleted; whatever is left at the
# end gets swept. Without it, a failed delete is forgotten and keeps its slice.
_vm_lock = threading.Lock()
_live_vms = {}


def track(vm):
    with _vm_lock:
        _live_vms[vm.id] = vm


def untrack(vm_id):
    with _vm_lock:
        _live_vms.pop(vm_id, None)


def delete(vm, attempts=6, floor=2.0):
    """vm.delete, retried; an already-gone VM counts as success.

    A VM holds its GPU slice until it is gone, so this is the one call worth
    retrying past what the SDK does on its own.

    Returns None once the VM is gone, or the last ArkerError if it never went.
    """
    last = None
    for i in range(attempts):
        try:
            vm.delete()
            untrack(vm.id)
            return None
        except ArkerError as e:
            if e.status == 404:
                untrack(vm.id)
                return None
            last = e
            # A 4xx other than 429 will not improve on retry.
            if e.status and 400 <= e.status < 500 and e.status != 429:
                break
            time.sleep(floor * (i + 1))
    return last


def sweep():
    """Delete every VM still alive at the end of the run.

    The per-cycle delete already retries, so anything here has survived several
    attempts - and this run is the only thing that knows these VMs exist.
    """
    with _vm_lock:
        left = [_live_vms[k] for k in sorted(_live_vms)]
    if not left:
        return
    print(f"\nsweeping {len(left)} agent VM(s) whose own delete did not land...")

    # Concurrent: run sequentially, the retry chains would outlast the test.
    def sweep_one(vm):
        err = delete(vm, attempts=10, floor=3.0)
        if err is None:
            ev("swept", vm_id=vm.id)
            print(f"   deleted {vm.id}")
        else:
            ev("sweep_failed", vm_id=vm.id, status=err.status, code=err.code)
            print(f"   FAILED  {vm.id} ({err.status} {err.code})")

    workers = [threading.Thread(target=sweep_one, args=(v,)) for v in left]
    for w in workers:
        w.start()
    for w in workers:
        w.join()


# ── events and the report ───────────────────────────────────────────────────

_events = []
_ev_lock = threading.Lock()


def ev(kind, **kw):
    """Record one thing that happened. Kept in memory; also appended to a log."""
    rec = {"t": time.time(), "ts": time.strftime("%H:%M:%S"), "kind": kind, **kw}
    with _ev_lock:
        _events.append(rec)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def report(window_min, cfg):
    rows = list(_events)
    rate = (lambda n: f"{n / window_min:.2f}/min") if window_min > 0 else (lambda n: "n/a")
    of = lambda k: [r for r in rows if r["kind"] == k]

    forks, rej = of("fork_ok"), of("fork_rejected")
    dels, del_fail = of("deleted"), of("delete_failed")
    swept, sweep_fail = of("swept"), of("sweep_failed")
    tasks = of("task_done")
    ok = [r for r in tasks if r.get("ok")]
    aband, lfail = of("task_abandoned"), of("task_launch_failed")
    threads = sorted({r["thread"] for r in rows if "thread" in r})

    print("\n" + "=" * 66)
    print(f"vLLM AGENT ITERATION - {cfg['threads']} threads, "
          f"{window_min:.0f} min window")
    print(f"1..{cfg['tests_per_agent']} feature-tests per agent session, "
          f"slice vgpu={cfg['vgpu']} ({cfg.get('vram_mib') or '?'} MiB resolved)")
    print("=" * 66)

    print("\nVM LIFECYCLE")
    print(f"  forks succeeded      : {len(forks)}   ({rate(len(forks))})")
    print(f"  503 no GPU slice     : {len([r for r in rej if r.get('status') == 503])}"
          f"   (none free when the fork asked)")
    print(f"  429 API concurrency  : {len([r for r in rej if r.get('status') == 429])}"
          f"   (your concurrency limit)")
    print(f"  deleted              : {len(dels)}"
          + (f"   ({len(del_fail)} still undeleted after retries)" if del_fail else ""))
    if swept:
        print(f"  swept at end         : {len(swept)}   (missed by their own cycle)")
    leaked = len(forks) - len(dels) - len(swept)
    if leaked > 0:
        print(f"  *** {leaked} VM(s) NOT deleted - each still holds a GPU slice ***")
        for r in sweep_fail:
            print(f"        {r.get('vm_id')}  ({r.get('status')} {r.get('code')})")
    if forks:
        lat = [r["fork_secs"] for r in forks]
        print(f"  fork latency         : p50={pct(lat,.5):.1f}s "
              f"p90={pct(lat,.9):.1f}s max={max(lat):.1f}s")
    if dels:
        lat = [r["delete_secs"] for r in dels]
        print(f"  delete latency       : p50={pct(lat,.5):.1f}s max={max(lat):.1f}s")

    print("\nAGENT TASKS")
    print(f"  launched             : {len(of('task_launched'))}")
    print(f"  SUCCEEDED (marker)   : {len(ok)}   ({rate(len(ok))})")
    print(f"  completed w/o marker : {len(tasks) - len(ok)}")
    print(f"  timed out            : {len(of('task_timeout'))}")
    if aband:
        print(f"  STILL RUNNING at end : {len(aband)}   "
              f"(max {max(r['waited'] for r in aband):.0f}s in flight)")
        print("      -> the window was too short for this workload, NOT a failure;")
        print("         raise --minutes or lower --per-test-budget")
    if lfail:
        print(f"  launch failed        : {len(lfail)}")
    if ok:
        lat = [r["total_secs"] for r in ok]
        print(f"  task wall time       : p50={pct(lat,.5):.0f}s "
              f"p90={pct(lat,.9):.0f}s max={max(lat):.0f}s")

    if not forks:
        print("\n*** NO VM WAS EVER FORKED - this is not a throughput measurement. ***")

    print("\nFAIRNESS - per thread (forks / ok tasks):")
    f_by = collections.Counter(r["thread"] for r in forks)
    o_by = collections.Counter(r["thread"] for r in ok)
    for t in threads:
        print(f"   thread {t:<2d}  forks={f_by.get(t,0):<4d} ok_tasks={o_by.get(t,0)}")

    if ok:
        feats = collections.Counter(f for r in ok for f in (r.get("features") or []))
        print("\nCRITICAL FEATURES exercised (across completed sessions):")
        for feat, c in feats.most_common():
            print(f"   {c:>3d}x  {str(feat)[:70]}")
    print("=" * 66 + "\n")


# ── argument parsing and teardown ──────────────────────────────────────────


def teardown_base(base, args):
    """Delete the base last - the agent VMs were forked from it."""
    if args.base_vm:
        print(f"\nbase {base.id} left alone (you passed --base-vm)")
        return
    if args.keep_base:
        print(f"\nbase {base.id} kept (--keep-base); reuse it with --base-vm {base.id}")
        return
    print(f"\n== delete base {base.id} ==")
    err = delete(base, attempts=10, floor=3.0)
    print("   deleted" if err is None
          else f"   FAILED ({err.status} {err.code}) - it still holds a GPU slice")


def parse_args():
    ap = argparse.ArgumentParser(
        description="Give every coding agent its own GPU VM, run them at once, "
                    "then throw the VMs away.")
    ap.add_argument("--minutes", type=float, default=10,
                    help="how long the agents keep iterating")
    ap.add_argument("--threads", type=int, default=8,
                    help="agents running at once (8 x 0.125 vGPU = one card)")
    ap.add_argument("--tests-per-agent", type=int, default=3, metavar="N",
                    help="each session runs a random 1..N vLLM feature tests")
    ap.add_argument("--vgpu", type=float, default=spec.VGPU,
                    help="GPU slice per VM, in eighths of a card (0.125 … 1.0)")
    ap.add_argument("--base-vm", metavar="ID",
                    help="reuse an existing base VM instead of building one")
    ap.add_argument("--keep-base", action="store_true",
                    help="do not delete the base at the end, so --base-vm can reuse it")
    # base build only
    ap.add_argument("--vcpu", type=int, default=2)
    ap.add_argument("--memory-mib", type=int, default=spec.MEMORY_MIB)
    ap.add_argument("--disk-mib", type=int, default=spec.DISK_MIB)
    ap.add_argument("--install-timeout", type=int, default=3600)
    ap.add_argument("--verify-timeout", type=int, default=900)
    # session pacing
    ap.add_argument("--session-timeout", type=int, default=1200)
    ap.add_argument("--per-test-budget", type=int, default=180,
                    help="per-test budget written into the prompt")
    ap.add_argument("--poll-secs", type=int, default=15)
    ap.add_argument("--grace", type=int, default=180,
                    help="how long past the deadline a task may still finish")
    args = ap.parse_args()
    if not (0.125 <= args.vgpu <= 1.0) or round(args.vgpu * 8) != args.vgpu * 8:
        raise SystemExit(f"--vgpu must be a multiple of 0.125 in [0.125, 1.0]; got {args.vgpu}")
    return args
