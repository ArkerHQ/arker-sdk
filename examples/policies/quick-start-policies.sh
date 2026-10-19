#!/usr/bin/env bash
#
# Policies as code - quick start (host-enforced egress policy)
#
# Forks a caller-selected source and attaches outbound rules (rewrite / deny /
# allow), enforced in the host network path so a process in the VM can't
# escape it.
#
# Prereqs: the Arker CLI (`bun add --global @arker-ai/cli`) and `jq`.

set -euo pipefail

export ARKER_API_KEY="${ARKER_API_KEY:-ark_live_...}"   # TODO: set your Arker API key
: "${ARKER_SOURCE_VM:?set ARKER_SOURCE_VM to a source name}"

VM=$(arker fork "$ARKER_SOURCE_VM" | jq -r .vm_id)
echo "forked $VM"
trap 'arker rm "$VM" >/dev/null 2>&1 || true' EXIT

# rewrite httpbin.org (inject a secret, strip the guest's authorization) · deny
# openai.com · allow the rest. The secret is stored on the host (encrypted,
# read back as ***); the guest never holds it.
arker update "$VM" --policies-file /dev/stdin >/dev/null <<'JSON'
{
  "secrets": {"DEMO_KEY": "sk-demo-the-guest-never-sees-this"},
  "rules": {
    "httpbin": {"type":"outbound","priority":300,"match":{"hosts":["httpbin.org"]},
                "action":{"rewrite":{"headers":{"x-api-key":"${secret:DEMO_KEY}"},"remove_headers":["authorization"]}}},
    "no-openai": {"type":"outbound","priority":200,"match":{"hosts":["openai.com"]},"action":"deny"},
    "rest": {"type":"outbound","priority":100,"action":"allow"}
  }
}
JSON
arker policies get "$VM" >/dev/null
echo "attached policy (rewrite httpbin · deny openai · allow *)"

echo "  rewrite httpbin.org (expect x-api-key = the secret, no authorization):"
arker run "$VM" "curl -s -H 'authorization: Bearer guest-token' https://httpbin.org/headers" \
  | grep -iE '"x-api-key"|"authorization"' || echo "    (none)"
CODE=$(arker run "$VM" "curl -s -m 8 -o /dev/null -w '%{http_code}' https://openai.com/ || true")
if [ -z "$CODE" ] || [ "$CODE" = "000" ]; then echo "  deny openai.com    : BLOCKED"; else echo "  deny openai.com    : REACHED $CODE"; fi
echo "  allow example.com  : $(arker run "$VM" "curl -s -m 8 -o /dev/null -w '%{http_code}' https://example.com/ || true")"
