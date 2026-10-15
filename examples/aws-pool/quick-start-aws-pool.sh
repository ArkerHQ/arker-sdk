#!/usr/bin/env bash
#
# AWS pool — quick start
#
# Creates a pool whose hosts Arker launches in your AWS account, waits for it to
# turn ready, forks a VM into it, and deletes the pool on exit.
#
# Prereqs: the Arker CLI (`bun add --global @arker-ai/cli`) with a saved
# provider and region, `jq`, and an AWS account with a subnet that routes to the
# internet (a NAT gateway, or an internet gateway with public IPv4 addresses).

set -euo pipefail

export ARKER_API_KEY="${ARKER_API_KEY:-ark_live_...}"   # TODO: set an admin Arker API key
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID to the account the hosts run in}"
: "${AWS_SUBNET_IDS:?set AWS_SUBNET_IDS to comma-separated subnets in that account}"

# create prints the pool on stdout and, on stderr, the link that sets up its role.
POOL=$(arker pools create --type aws --platform icelake --name quick-start-aws \
  --vcpu 120 --memory-mib 245760 --disk-mib 1048576 \
  --aws-account-id "$AWS_ACCOUNT_ID" --subnet-ids "$AWS_SUBNET_IDS" | jq -r .pool_id)
trap 'arker pools rm "$POOL" >/dev/null 2>&1 || true' EXIT

echo "waiting for $POOL: open the link above and create the stack"
while true; do
  POOL_JSON=$(arker pools get "$POOL")
  [ "$(jq -r .state <<<"$POOL_JSON")" = ready ] && break
  echo "  $(jq -r '.state_detail // "waiting for hosts to join"' <<<"$POOL_JSON")"
  sleep 30
done

VM=$(arker fork --image ubuntu:24.04 --pool-id "$POOL" | jq -r .vm_id)
echo "forked $VM into $POOL"
arker run "$VM" uname -a
