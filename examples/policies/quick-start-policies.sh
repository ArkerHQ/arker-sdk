#!/usr/bin/env bash
#
# Policies as code - quick start (host-enforced egress policy)
#
# Forks a caller-selected source and attaches an ordered list of outbound
# rules (allow / deny / rewrite / gate), enforced in the host network path so a
# process in the VM can't escape it.
#
# Prereqs: the Arker CLI (`bun add --global @arker-ai/cli`) and `jq`.

set -euo pipefail

export ARKER_API_KEY="${ARKER_API_KEY:-ark_live_...}"   # TODO: set your Arker API key
: "${ARKER_SOURCE_VM:?set ARKER_SOURCE_VM to a source name}"

VM=$(arker fork "$ARKER_SOURCE_VM" | jq -r .vm_id)
echo "forked $VM"
trap 'arker rm "$VM" >/dev/null 2>&1 || true' EXIT

# rewrite httpbin.org (inject a secret, strip the guest's authorization) · deny
# openai.com · allow the rest. The injected credential is stored on the host
# (encrypted, redacted on read); the guest never sees it.
arker policies set "$VM" >/dev/null <<'JSON'
{
    "policies": [
      {"type":"outbound","match":{"hosts":["httpbin.org"]},
       "action":{"rewrite":{"headers":{"x-api-key":"sk-demo-the-guest-never-sees-this"},"remove_headers":["authorization"]}}},
      {"type":"outbound","match":{"hosts":["openai.com"]},"action":"deny"},
      {"type":"outbound","action":"allow"}
    ]
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
