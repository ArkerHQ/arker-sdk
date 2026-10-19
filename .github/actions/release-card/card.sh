#!/usr/bin/env bash
# One Slack card per SDK release: posted when a publish starts, edited once
# with the outcome. Editing needs the message ts, hence chat.postMessage/update
# with the bot token rather than a webhook.
#
# Env: STATE PACKAGE VERSION REGISTRY_URL TS CHANNEL, plus GitHub's own
# GITHUB_SHA GITHUB_ACTOR GITHUB_SERVER_URL GITHUB_REPOSITORY GITHUB_RUN_ID.
# CARD_DRY_RUN=1 prints the payload and touches neither AWS nor Slack.
set -euo pipefail

TOKEN_PARAMETER=/arkerd/staging-aws-us-west-2/test-runner/slack-bot-token
# chat.postMessage resolves a name; chat.update needs the ID the post returned.
POST_CHANNEL='#arker-cd'

render() {
  local icon color status
  local run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
  case "$STATE" in
    publishing) icon=🚀 color='#1d9bd1' status=publishing ;;
    published)  icon=✅ color='#2eb67d' status="published · <$REGISTRY_URL|registry link>" ;;
    failed)     icon=❌ color='#e01e5a' status="failed · <$run_url|run>" ;;
    cancelled)  icon=⚠️ color='#ecb22e' status="cancelled · <$run_url|run>" ;;
    *) echo "unknown state: $STATE" >&2; return 1 ;;
  esac
  jq -n \
    --arg icon "$icon" --arg color "$color" --arg status "$status" \
    --arg pkg "$PACKAGE" --arg version "$VERSION" --arg state "$STATE" \
    --arg sha "$GITHUB_SHA" --arg actor "$GITHUB_ACTOR" --arg run_url "$run_url" \
    --arg repo_web "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" '
    "arker-sdk \($pkg) \($version) \($state)" as $fallback
    | {text: $fallback,
       attachments: [{color: $color, fallback: $fallback, blocks: [
         {type: "section", text: {type: "mrkdwn",
           text: "\($icon) *arker-sdk \($pkg)* \($version) ← <\($repo_web)/commit/\($sha)|`\($sha[0:7])`>"}},
         {type: "context", elements: [{type: "mrkdwn", text: "\($actor) · <\($run_url)|run>"}]},
         {type: "section", text: {type: "mrkdwn", text: $status}}]}]}'
}

if [ "${CARD_DRY_RUN:-}" = 1 ]; then
  render
  exit 0
fi

token=$(aws ssm get-parameter --name "$TOKEN_PARAMETER" --with-decryption \
  --query Parameter.Value --output text)
echo "::add-mask::$token"

if [ -z "${TS:-}" ]; then
  method=chat.postMessage target=$POST_CHANNEL edit_ts=
else
  method=chat.update target=${CHANNEL:?} edit_ts=$TS
fi

# Slack answers 200 with ok:false for a bad channel or revoked token.
body=$(render | jq --arg channel "$target" --arg ts "$edit_ts" \
    '. + {channel: $channel} + (if $ts == "" then {} else {ts: $ts} end)' \
  | curl -sS --fail-with-body --max-time 15 -X POST "https://slack.com/api/$method" \
      -H "Authorization: Bearer $token" \
      -H 'Content-Type: application/json; charset=utf-8' \
      --data @-) || { echo "slack $method: request failed" >&2; exit 1; }

if [ "$(jq -r '.ok' <<<"$body")" != true ]; then
  echo "slack $method: $(jq -r '.error // "unknown"' <<<"$body")" >&2
  exit 1
fi

{
  echo "ts=$(jq -r '.ts' <<<"$body")"
  echo "channel=$(jq -r '.channel' <<<"$body")"
} >> "$GITHUB_OUTPUT"
