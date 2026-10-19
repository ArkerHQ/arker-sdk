# Inference engine development

This example forks vLLM across fractional-vGPU VMs and assigns one test to each coding agent.

## Quickstart

```bash
export ARKER_API_KEY=<your-arker-api-key>
export ARKER_ANTHROPIC_API_KEY=sk-ant-...             # your own anthropic API key,
                                                      # injected by policy, never seen by a guest
# Only identity-linked keys require this optional workspace header:
# export ARKER_ANTHROPIC_WORKSPACE_ID=wrkspc_...

uv run --with arker python launch.py --minutes 10 --threads 8 --tests-per-agent 3
```

## Why

Fractional vGPUs let multiple agents test inference-engine changes concurrently without allocating a full GPU to each.
