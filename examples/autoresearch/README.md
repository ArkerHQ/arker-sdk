# Autoresearch

This example runs multiple Arker VMs, each with a fractional vGPU and each running a coding agent tasked with tuning a small model.

## Quickstart

Set `VGPUs`, the fraction (0.25 achieves higher throughput) of a vGPU (we use H100 as the platform for VMs in this example) and `AGENTS` and `TURNS` for the scale and length of the research.

Run:

```bash
export ARKER_API_KEY=ark_live_...
export ARKER_BASE_URL=https://arker-us-west.arker.ai/api
export OPENROUTER_API_KEY=sk-or-v1-...

# Quick test: 2 agents, 2 turns each, ~5 minutes
AGENTS=2 TURNS=2 VGPUS=0.25 \
  uv run --with arker --with matplotlib python autoresearch.py

# Full test: compare the throughput of running with full vGPUs and with fraction vGPUs
AGENTS=4 TURNS=8 VGPUS=0.25,1.0 \
  uv run --with arker --with matplotlib python autoresearch.py
```

Result:

```
results -> results/20260819-194048-4agents-2turns
[19:40:49] === vgpu0.25: 4 agents x 0.25 vGPU, 2 turns each ===
[19:44:31] prep ready — forking 4 agents
[19:44:33] agent1 turn 1/2 started
[19:45:01] agent1 turn 1/2 done in 28s — val_loss 3.6545
```

To take a closer look at how Arker works, read `autoresearch.py` and read the [docs](https://arker.ai/docs) for `fork`, `run`, `sync` for the general primitives that make GPU virtualization work.
