#!/usr/bin/env bash
#
# Browser checkpoints — quick start
#
# fork snapshots a machine's live memory, so forking a running browser
# checkpoints it mid-session. Here we open two Wikipedia pages and fork at each,
# leaving two live checkpoints you can reopen over VNC.
#
# Prereqs: the Arker CLI (`bun add --global @arker-ai/cli`) and `jq`.

set -euo pipefail

export ARKER_API_KEY="${ARKER_API_KEY:-ark_live_...}"   # TODO: set your Arker API key
: "${ARKER_SOURCE_VM:?set ARKER_SOURCE_VM to a source with a desktop}"
REGION="${ARKER_REGION:-us-west-2}"

# The selected source must have a desktop. Add Chromium from the arm64 xtradeb PPA.
VM=$(arker fork "$ARKER_SOURCE_VM" | jq -r .vm_id)
echo "forked $VM"
trap 'arker rm "$VM" >/dev/null 2>&1 || true' EXIT
arker run --timeout 480000 "$VM" "export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq software-properties-common && add-apt-repository -y ppa:xtradeb/apps && apt-get update -qq && apt-get install -y -qq chromium" >/dev/null 2>&1 || true
until arker run "$VM" "command -v chromium" >/dev/null 2>&1; do sleep 5; done

# open <url> fullscreen on the VNC desktop (display :99)
arker sync "$VM" /usr/local/bin/open-url <<'SH'
#!/usr/bin/env bash
# HOME is left unset here so it inherits the VM's default account home
# directory rather than hardcoding one.
export DISPLAY=:99
pkill -f chromium 2>/dev/null; sleep 1
nohup chromium --kiosk --no-sandbox --no-first-run --disable-gpu --disable-dev-shm-usage --user-data-dir=/tmp/c "$1" >/dev/null 2>&1 &
until xdotool search --class chromium >/dev/null 2>&1; do sleep 0.5; done
sleep 3; wid=$(xdotool search --class chromium | tail -1); xdotool windowraise "$wid"
SH
arker run "$VM" "chmod +x /usr/local/bin/open-url" >/dev/null

# open a page, fork a checkpoint, make it reachable, print its noVNC URL
checkpoint() {
  arker run --time-to-background 0 "$VM" "open-url '$1'" >/dev/null
  sleep 4
  local ck
  ck=$(arker fork --source-vm-id "$VM" | jq -r .vm_id)
  arker policies set "$ck" >/dev/null <<'JSON'
{
  "policies": [
    {"type":"inbound","match":{"ports":[6080]},"action":"allow","auth":"open"}
  ]
}
JSON
  echo "$2: https://$ck-6080.aws-$REGION.arker.app/vnc.html"
}
checkpoint "https://en.wikipedia.org/wiki/Virtual_machine" "checkpoint A"
checkpoint "https://en.wikipedia.org/wiki/Firecracker_(software)" "checkpoint B"
