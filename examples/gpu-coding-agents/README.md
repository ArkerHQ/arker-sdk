# Inference engine development

In this example, we launch multiple Arker VMs each with a fractional vGPU and each tasked with some unit of work in the lifecycle of inference engine development. For simplicity, each agent is given a fork of vLLM and is instructed simply to run a test.

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

Recursive self-improvement. If inference is a critical part of your stack, there is a threshold upon which optimizing the inference engine itself becomes valuable. vGPUs provide an efficient way to deploy agents for the task of inference engine optimization itself.
