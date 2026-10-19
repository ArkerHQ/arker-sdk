#!/usr/bin/env python3

import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import base_spec as spec  # noqa: E402
import helpers as h  # noqa: E402
from arker import ArkerError  # noqa: E402

VERIFY_TASK = (
    "Create a file named toolcheck.py containing exactly "
    "print('ARKER_AGENT_OK'), then run it with python3 and show the output."
)


# ── the base VM ─────────────────────────────────────────────────────────────


def build_base(ar, args, real_key, workspace_id):
    """Fork the public GPU template and turn it into the image agents fork from.
    """
    base = ar.fork(
        source_vm_name=spec.GOLDEN,
        source_org_name=spec.GOLDEN_ORG or None,
        name=f"{spec.PREFIX}-base",
        platforms=spec.PLATFORMS or None,
        policies=spec.policy_doc(real_key, workspace_id),
        resources={
            "vcpu": args.vcpu,
            "memory_mib": args.memory_mib,
            "disk_mib": args.disk_mib,
            "vgpu": args.vgpu,
        },
    )
    try:
        return _prepare_base(base, args, real_key)
    except BaseException:
        # A half-built base is no use to anyone and still holds a slice.
        print(f"\nbase build failed — deleting {base.id}")
        h.delete(base, attempts=10, floor=3.0)
        raise


def _prepare_base(base, args, real_key):
    vram_mib = h.slice_mib(base)

    stored = base.get_policies()
    if real_key in str(stored.secrets):
        raise SystemExit("FAIL: the real key came back UNMASKED — stop.")

    h.run(base, spec.ENV_SH.format(key=spec.DUMMY_KEY), session_idx=3, timeout=90)

    # Detached and polled: the install far outlives a foreground run's window.
    h.run(base, spec.detach(spec.INSTALL, "install"), session_idx=2, timeout=120)
    rc, _ = h.wait_marker(base, "install", args.install_timeout, "install")
    log = h.stdout_of(h.run(base, spec.collect_cmd("install", 25), session_idx=2, timeout=90))
    if rc is None:
        raise SystemExit(f"FAIL: install did not finish within {args.install_timeout}s.\n"
                         + log.strip()[-1200:])

    check = h.stdout_of(h.run(base, spec.TOOLCHAIN_CHECK, session_idx=2, timeout=180))
    for missing in ("NO_CLAUDE", "NO_VLLM", "NO_MODEL"):
        if missing in check:
            raise SystemExit(f"FAIL: {missing} — base is not usable.")

    # The agent must write AND run a file: a chat-only prompt would pass even
    # with tools broken, and a 401 here means the rewrite never applied.
    h.run(base, spec.detach(spec.claude_script(VERIFY_TASK, "verify"), "verify"),
          session_idx=1, timeout=120)
    rc, secs = h.wait_marker(base, "verify", args.verify_timeout, "verify", poll_s=15)
    out = h.stdout_of(h.run(base, spec.collect_cmd("verify", 40), session_idx=1, timeout=90))
    print(f"   Base VM finished after {secs}s rc={rc}")
    print("   " + out.strip().replace("\n", "\n   ")[-1000:])
    if "ARKER_AGENT_OK" not in out:
        raise SystemExit(
            "FAIL: the agent did not complete a tool-using task.\n"
            "  401 -> the policy rewrite did not apply; check ARKER_ANTHROPIC_API_KEY.\n"
            "  tool/permission errors -> IS_SANDBOX=1 missing from env.sh.")

    h.run(base, spec.detach(spec.VLLM_SMOKE, "vllm"), session_idx=2, timeout=120)
    rc, secs = h.wait_marker(base, "vllm", args.verify_timeout, "vllm-smoke", poll_s=15)
    vout = h.stdout_of(h.run(base, spec.collect_cmd("vllm", 30), session_idx=2, timeout=90))
    if spec.VLLM_OK_MARKER not in vout:
        raise SystemExit("FAIL: vLLM did not initialize on the GPU.")

    print(f"\nbase ready: {base.id} ({vram_mib} MiB slice)\n")
    return base, vram_mib


# ── one agent ───────────────────────────────────────────────────────────────


def run_agent(ar, base, policy, thread_idx, cycle, args, base_vram_mib, stop, deadline):
    """Fork a VM off the base, run ONE agent session on it, then delete it.
    """
    name = f"{spec.PREFIX}-t{thread_idx}-c{cycle}"
    t_fork = time.time()
    try:
        vm = base.fork(
            name=name,
            resources={"vgpu": args.vgpu, "disk_mib": args.disk_mib},
            policies=policy,
        )
    except ArkerError as e:
        # 429 = your API concurrency limit; the fork never reached a GPU.
        # 503 = no GPU slice free — often the previous VM's slice is still held.
        h.ev("fork_rejected", thread=thread_idx, cycle=cycle, status=e.status,
             code=e.code, waited=round(time.time() - t_fork, 1))
        stop.wait(5.0 + random.random() * 5.0)
        return
    h.track(vm)
    vram_mib = h.slice_mib(vm, base_vram_mib)
    h.ev("fork_ok", thread=thread_idx, cycle=cycle, vm_id=vm.id, name=name,
         fork_secs=round(time.time() - t_fork, 1))

    try:
        if stop.is_set() or time.time() > deadline:
            return
        # A different slice of the feature list per session, so the fleet is not
        # N copies of one workload.
        rng = random.Random(9000 + thread_idx + cycle)
        n = rng.randint(1, max(1, args.tests_per_agent))
        features = [spec.CRITICAL_FEATURES[(thread_idx + cycle + i) % len(spec.CRITICAL_FEATURES)]
                    for i in range(n)]
        agent_session(vm, thread_idx, cycle, features, args, stop, deadline, vram_mib)
    finally:
        # Always delete, even on interrupt — a leaked VM holds its GPU slice.
        t_del = time.time()
        err = h.delete(vm)
        if err is None:
            h.ev("deleted", thread=thread_idx, cycle=cycle, vm_id=vm.id,
                 delete_secs=round(time.time() - t_del, 1))
        else:
            h.ev("delete_failed", thread=thread_idx, cycle=cycle, vm_id=vm.id,
                 status=err.status, code=err.code)


def agent_session(vm, thread_idx, cycle, features, args, stop, deadline, vram_mib):
    """Launch Claude Code on the VM and wait for it to finish its tests.
    """
    tag = f"sess{cycle}"
    task = spec.feature_test_task(features, vram_mib, args.per_test_budget)
    t0 = time.time()
    try:
        h.run(vm, spec.detach(spec.claude_script(task, tag), tag), session_idx=1, timeout=120)
    except ArkerError as e:
        h.ev("task_launch_failed", thread=thread_idx, cycle=cycle,
             vm_id=vm.id, status=e.status, code=e.code)
        return
    h.ev("task_launched", thread=thread_idx, cycle=cycle, vm_id=vm.id,
         n_tests=len(features), features=features)

    while not stop.is_set():
        stop.wait(args.poll_secs)
        elapsed = time.time() - t0
        if elapsed > args.session_timeout or time.time() > deadline + args.grace:
            h.ev("task_timeout", thread=thread_idx, cycle=cycle, vm_id=vm.id,
                 features=features, waited=round(elapsed, 1))
            return
        try:
            done = h.stdout_of(h.run(vm, spec.poll_cmd(tag), session_idx=4, timeout=60)).strip()
        except ArkerError as e:
            h.ev("poll_error", thread=thread_idx, vm_id=vm.id, status=e.status)
            continue
        if not done or "RUNNING" in done:
            continue

        log = ""
        try:
            log = h.stdout_of(h.run(vm, spec.collect_cmd(tag, 40), session_idx=4, timeout=90))
        except ArkerError:
            pass
        h.ev("task_done", thread=thread_idx, cycle=cycle, vm_id=vm.id,
             rc=done.split()[0], ok=spec.MARKER in log,
             total_secs=round(time.time() - t0, 1),
             features=features, out=log[-500:])
        return
    h.ev("task_abandoned", thread=thread_idx, cycle=cycle, vm_id=vm.id,
         features=features, waited=round(time.time() - t0, 1))


def iterate(ar, base, policy, thread_idx, args, base_vram_mib, stop, deadline):
    """One thread: fork -> agent -> delete, over and over until time runs out."""
    cycle = 0
    while not stop.is_set() and time.time() < deadline:
        cycle += 1
        run_agent(ar, base, policy, thread_idx, cycle, args, base_vram_mib, stop, deadline)


# ── the run ─────────────────────────────────────────────────────────────────


def main():
    args = h.parse_args()
    real_key = os.environ.get("ARKER_ANTHROPIC_API_KEY", "")
    workspace_id = os.environ.get("ARKER_ANTHROPIC_WORKSPACE_ID", "")
    if not real_key or not os.environ.get("ARKER_API_KEY"):
        raise SystemExit(
            "missing env:\n"
            "  export ARKER_API_KEY=<your-arker-api-key>\n"
            "  export ARKER_ANTHROPIC_API_KEY=sk-ant-...   # never reaches a guest")

    ar = h.client()
    policy = spec.policy_doc(real_key, workspace_id)
    print(f"placement: {h.PROVIDER}/{h.REGION}\n")

    if args.base_vm:
        base = ar.vm(args.base_vm).refresh()
        base_vram_mib = h.slice_mib(base)
        print(f"reusing base {base.id} ({base_vram_mib} MiB slice)\n")
    else:
        base, base_vram_mib = build_base(ar, args, real_key, workspace_id)

    print(f"== iterate: {args.threads} agents x {args.minutes} min ==")
    h.ev("start", threads=args.threads, minutes=args.minutes,
         tests_per_agent=args.tests_per_agent, vgpu=args.vgpu, vram_mib=base_vram_mib)

    deadline = time.time() + args.minutes * 60
    stop = threading.Event()
    threads = [threading.Thread(target=iterate, daemon=True,
                                args=(ar, base, policy, i, args, base_vram_mib, stop, deadline))
               for i in range(args.threads)]
    try:
        for t in threads:
            t.start()
        while time.time() < deadline and any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        print("\ninterrupted — draining and deleting VMs")
    finally:
        stop.set()
        # Generous: threads are mid-cycle and must still delete their VM.
        for t in threads:
            t.join(timeout=args.grace + 120)
        h.ev("end")
        h.sweep()                       # agent VMs whose own delete failed
        h.teardown_base(base, args)

    h.report(args.minutes, {"threads": args.threads, "vgpu": args.vgpu,
                            "tests_per_agent": args.tests_per_agent,
                            "vram_mib": base_vram_mib})


if __name__ == "__main__":
    main()
