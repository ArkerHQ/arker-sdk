"""What runs INSIDE the VMs: the outbound policy, the install, the agent task.

Content only - no SDK calls. launch.py is the flow that feeds these to
`vm.run()`; helpers.py is the plumbing around it.

The guest never holds a usable key:

    guest env  ANTHROPIC_API_KEY = DUMMY_KEY        (worthless if exfiltrated)
    policy     rewrite x-api-key -> ${secret:...}   (your real key)
"""

import base64
import os

GOLDEN = os.environ.get("FLEET_GOLDEN", "ubuntu-gpu")
# ubuntu-gpu is a public template, so its owning org must be named.
GOLDEN_ORG = os.environ.get("FLEET_GOLDEN_ORG", "ArkerHQ")
# Only the base fork picks a platform; children inherit what it landed on, so one
# card type serves a whole run. Comma-separated to allow more than one.
PLATFORMS = [p for p in os.environ.get(
    "FLEET_PLATFORMS", "x86_64-a100sxm-80gb").split(",") if p]
PREFIX = os.environ.get("FLEET_PREFIX", "vllm-agent")

# Valid-looking but worthless. Must LOOK like a key: a blank one fails as
# "no API key", which would mask the failure this detects (rewrite not applied).
DUMMY_KEY = "invalid-test-key"

# Referenced as ${secret:NAME} in the rewrite.
SECRET_NAME = "ANTHROPIC_API_KEY"

# Small on purpose: a larger model fills the slice on load and every task OOMs.
MODEL_ID = os.environ.get("FLEET_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
MODEL_DIR = "/opt/models/vllm-test"

# vLLM + torch + the staged model need ~12 GiB; these are also ubuntu-gpu's
# floors, so a fork below either is rejected.
DISK_MIB = int(os.environ.get("FLEET_DISK_MIB", "102400"))
MEMORY_MIB = int(os.environ.get("FLEET_MEMORY_MIB", "16384"))

# GPU slice per VM, in eighths of one card. At 0.125 the default 8 threads fill
# one card between them.
VGPU = float(os.environ.get("FLEET_VGPU", "0.125"))

WORK = "/home/user/work"
AGENT = "/root/agent"

def policy_doc(real_key, workspace_id=""):
    """Rewrite the Anthropic auth header; allow everything else.

    First match wins. The catch-all `allow` is load-bearing: without it a
    non-empty document denies pip and HuggingFace, and the install fails.
    """
    headers = {"x-api-key": "${secret:%s}" % SECRET_NAME}
    if workspace_id:
        headers["anthropic-workspace-id"] = workspace_id

    return {
        "policies": [
            {
                "type": "outbound",
                "match": {"hosts": ["api.anthropic.com"]},
                "action": {
                    "rewrite": {
                        "headers": headers
                    }
                },
            },
            {"type": "outbound", "action": "allow"},
        ],
        "secrets": {SECRET_NAME: real_key},
    }


# ── base image build ────────────────────────────────────────────────────────
# Runs detached and is polled via a marker file - vLLM pulls several GB, far
# longer than a foreground run's window. ubuntu-gpu already ships Claude Code,
# build-essential and pip, so this adds only what it lacks.
INSTALL = r"""
set -x
export DEBIAN_FRONTEND=noninteractive
mkdir -p {AGENT} {WORK} {MODEL_DIR}
echo "INSTALL_START $(date -u +%FT%TZ)"
df -h /
python3 -V
# Triton JIT-compiles at engine init and needs <Python.h>.
DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-dev \
  && echo PYDEV_OK || echo PYDEV_FAILED
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true

# Claude Code ships with the golden - checked, not installed, so a golden that
# drops it fails here rather than at the first agent task.
export PATH="$PATH:/usr/local/bin:/root/.local/bin"
command -v claude && claude --version || echo CLAUDE_MISSING

pip3 install --break-system-packages --no-cache-dir -q vllm \
  && echo VLLM_OK || echo VLLM_FAILED
pip3 install --break-system-packages --no-cache-dir -q huggingface_hub \
  && echo HFHUB_OK || echo HFHUB_FAILED

# Staged into the base rootfs so every child inherits it and no task downloads.
python3 - <<'PYSTAGE'
import time
from huggingface_hub import snapshot_download
# Retried: a dropped TLS connection part-way through ~1 GB is common enough, and
# snapshot_download resumes from cache, so a retry is cheap.
for attempt in range(4):
    try:
        p = snapshot_download("{MODEL_ID}", local_dir="{MODEL_DIR}")
        print("MODEL_STAGED", p)
        break
    except Exception as e:
        print("MODEL_STAGE_RETRY", attempt + 1, type(e).__name__, e)
        time.sleep(5 * (attempt + 1))
else:
    print("MODEL_STAGE_FAILED")
PYSTAGE

python3 -c "import torch;print('TORCH',torch.__version__,'CUDA',torch.cuda.is_available())" || true
python3 -c "import vllm;print('VLLM',vllm.__version__)" || echo VLLM_IMPORT_FAILED
df -h /
echo "INSTALL_DONE $(date -u +%FT%TZ)"
""".replace("{AGENT}", AGENT).replace("{WORK}", WORK) \
   .replace("{MODEL_DIR}", MODEL_DIR).replace("{MODEL_ID}", MODEL_ID)


# IS_SANDBOX=1: Claude Code refuses --dangerously-skip-permissions as root
# without it, so every "write and run a script" task fails on tool permissions.
# HF_HUB_OFFLINE=1: model is staged, so skip vLLM's per-init HuggingFace metadata
# round-trip.
ENV_SH = (
    "mkdir -p {AGENT} {WORK} && "
    "printf 'export ANTHROPIC_API_KEY=%s\\n"
    "export PATH=$PATH:/home/user/.local/bin:/root/.local/bin\\n"
    "export IS_SANDBOX=1\\n"
    "export HF_HUB_OFFLINE=1\\n"
    "export VLLM_USE_FLASHINFER_SAMPLER=0\\n"
    "export VLLM_MODEL_DIR=%s\\n' '{key}' '{model_dir}' > {AGENT}/env.sh && "
    "chmod 600 {AGENT}/env.sh && "
    "(grep -q agent/env.sh /root/.bashrc 2>/dev/null || "
    "echo '. {AGENT}/env.sh' >> /root/.bashrc); echo SEEDED"
).replace("{AGENT}", AGENT).replace("{WORK}", WORK).replace("{model_dir}", MODEL_DIR)


# ── the coding task ─────────────────────────────────────────────────────────
# Each session gets a different slice of these, so the fleet is not eight copies
# of one workload.
CRITICAL_FEATURES = [
    "batched offline generation: LLM.generate over a batch of prompts returns "
    "exactly one completion per prompt, in the same order",
    "greedy determinism: temperature=0 (or top_k=1) yields identical output "
    "across two runs of the same prompt",
    "max_tokens is honored: the generated token count never exceeds the "
    "requested max_tokens",
    "stop strings: generation halts at the first stop string and the stop text "
    "is not emitted past it",
    "long-context handling: a prompt near max_model_len still generates without "
    "error and respects the remaining token budget",
    "tokenizer round-trip: the served model's tokenizer encodes+decodes text "
    "consistently and handles special tokens",
    "concurrent batch mapping: a large batch of distinct prompts all complete "
    "and each result maps back to its own input",
    "sampling controls: higher temperature/top_p visibly diverges from greedy, "
    "and a fixed seed makes sampling reproducible",
]

MARKER = "ARKER_TESTS_DONE"

# Distinct from MARKER so a task's output can't be mistaken for the build gate.
VLLM_OK_MARKER = "VLLM_ENGINE_OK"

# Proves the base can STAND UP vLLM on the slice, not merely import it - the JIT
# gaps only surface on a real engine init.
VLLM_SMOKE = (
    ". {AGENT}/env.sh\n"
    "mkdir -p {WORK}; cd {WORK}\n"
    # A real file, not stdin/-c: vLLM spawns EngineCore as a subprocess that
    # re-locates the parent's __main__.
    "cat > {WORK}/_vllm_smoke.py <<'PYV'\n"
    "import subprocess\n"
    "def vram_used_mib():\n"
    "    # vLLM allocates in a child process, so torch here would read ~0.\n"
    "    out = subprocess.check_output(['nvidia-smi','--query-gpu=memory.used',\n"
    "        '--format=csv,noheader,nounits']).decode().strip().splitlines()[0]\n"
    "    return int(out)\n"
    "def main():\n"
    "    before = vram_used_mib()\n"
    "    from vllm import LLM, SamplingParams\n"
    "    llm = LLM(model='{MODEL_DIR}', gpu_memory_utilization=0.55,\n"
    "              max_model_len=1024, enforce_eager=True)\n"
    "    out = llm.generate(['hello']*4, SamplingParams(max_tokens=8))\n"
    "    print('VLLM_VRAM_USED_MIB', vram_used_mib() - before)\n"
    "    print('{MARKER_OK}', len(out))\n"
    "if __name__ == '__main__':\n"
    "    main()\n"
    "PYV\n"
    "python3 -u {WORK}/_vllm_smoke.py\n"
).replace("{AGENT}", AGENT).replace("{WORK}", WORK) \
 .replace("{MODEL_DIR}", MODEL_DIR).replace("{MARKER_OK}", VLLM_OK_MARKER)


# Is the base actually usable? Checked for real weights, not just the directory:
# INSTALL creates MODEL_DIR up front, so `test -d` would pass on a failed download.
TOOLCHAIN_CHECK = (
    f". {AGENT}/env.sh; "
    f"command -v claude >/dev/null && echo CLAUDE_OK || echo NO_CLAUDE; "
    f"python3 -c 'import vllm' 2>/dev/null && echo VLLM_OK || echo NO_VLLM; "
    f"test -s {MODEL_DIR}/config.json "
    f"&& ( ls {MODEL_DIR}/*.safetensors >/dev/null 2>&1 "
    f"|| ls {MODEL_DIR}/*.bin >/dev/null 2>&1 ) "
    f"&& echo MODEL_OK || echo NO_MODEL"
)


def feature_test_task(features, vram_mib, per_test_budget):
    """The prompt for ONE agent session: one test per feature, PASS/FAIL each.

    The prompt insists a real vLLM error is a legitimate FAIL, so the agent does
    not write trivially-passing tests or skip loading the GPU.
    """
    numbered = "\n".join(f"  {i+1}. {f}" for i, f in enumerate(features))
    n = len(features)
    return (
        f"You are validating a local vLLM install on an NVIDIA GPU slice of "
        f"{vram_mib} MiB. The model is ALREADY on disk at {MODEL_DIR} - load it "
        f"from that path with vLLM's offline LLM API and never download anything.\n\n"
        f"Write and run {n} independent tests, one per critical vLLM feature below, "
        f"in order:\n{numbered}\n\n"
        f"For EACH test:\n"
        f"- Write it to its OWN new .py FILE under {WORK} (a real __main__ file - "
        f"NOT `python3 -c` or a heredoc: vLLM spawns a subprocess that needs a real "
        f"file on disk).\n"
        f"- It MUST load the model onto the GPU via vLLM's offline `LLM` API and "
        f"actually exercise that feature; a test that never loads the model on the "
        f"GPU is a failure of the test, not a pass.\n"
        f"- Run it with python3, capture the output, and decide PASS or FAIL with a "
        f"one-line reason. A real vLLM error/crash is a legitimate FAIL - report it "
        f"honestly, do not design the test to avoid it.\n"
        f"- Read device VRAM with `nvidia-smi --query-gpu=memory.used "
        f"--format=csv` (the engine runs in a CHILD process, so "
        f"torch.cuda.mem_get_info() in your own script reads ~0).\n"
        f"- Keep each test under {per_test_budget} seconds.\n\n"
        f"After all {n} tests, print {MARKER} followed by a one-line summary: "
        f"how many PASSED and how many FAILED (e.g. `{MARKER} 3/4 passed`)."
    )

def claude_script(task, tag):
    """In-guest shell script for one agent task.

    The prompt travels base64-encoded; interpolated raw it would truncate at the
    first apostrophe or `$`.
    """
    b64 = base64.b64encode(task.encode()).decode()
    return (
        f". {AGENT}/env.sh\n"
        f"mkdir -p {WORK}\n"
        f"cd {WORK}\n"
        f"printf %s {b64} | base64 -d > {AGENT}/{tag}.prompt\n"
        f'claude -p "$(cat {AGENT}/{tag}.prompt)" --output-format text '
        f"--dangerously-skip-permissions\n"
    )


def detach(script, tag):
    """Launch an in-guest script detached, to outlive a foreground run's window."""
    b64 = base64.b64encode(script.encode()).decode()
    # The marker write lives in the wrapper, not the script: a script that forgets
    # it makes every poll time out, with no way to tell "running" from "died".
    return (
        f"mkdir -p {AGENT} {WORK}; rm -f {AGENT}/{tag}.done {AGENT}/{tag}.log; "
        f"printf %s {b64} | base64 -d > {AGENT}/{tag}.sh; "
        f"setsid nohup bash -c 'bash {AGENT}/{tag}.sh; echo $? > {AGENT}/{tag}.done' "
        f">{AGENT}/{tag}.log 2>&1 & "
        f"sleep 1; echo LAUNCHED"
    )


def poll_cmd(tag):
    """Cheap marker check - small enough to return well inside the run window."""
    return f"cat {AGENT}/{tag}.done 2>/dev/null || echo RUNNING"


def collect_cmd(tag, tail=40):
    return f"tail -{tail} {AGENT}/{tag}.log 2>/dev/null || echo NOLOG"
