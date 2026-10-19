const cliCommands = String.raw`# 1. Configure Arker
export ARKER_API_KEY=ark_live_...
export ARKER_PROVIDER=aws
export ARKER_REGION=us-west-2
test -n "$ARKER_SOURCE_VM" || { echo "set ARKER_SOURCE_VM to a source name" >&2; exit 1; }

# 2. Fork the selected source VM
VM=$(arker fork "$ARKER_SOURCE_VM" | jq -r .vm_id)

# 3. Allow only npm during the build
arker update "$VM" --policies-file /dev/stdin >/dev/null <<'JSON'
{
  "rules": {
    "npm": {
      "type": "outbound",
      "priority": 100,
      "match": {
        "hosts": ["registry.npmjs.org"],
        "ports": [443]
      },
      "action": "allow"
    }
  }
}
JSON

# 4. Sync the React project into the VM
tar --exclude=node_modules --exclude=dist -C ./examples/react-policy/app -czf - . \
  | arker sync "$VM" /tmp/react-policy.tgz
arker run "$VM" "mkdir -p /workspace/react-policy && tar -xzf /tmp/react-policy.tgz -C /workspace/react-policy"

# 5. Build the React project inside the VM
arker run "$VM" "cd /workspace/react-policy && npm ci --include=dev --no-audit --no-fund"
arker run "$VM" "cd /workspace/react-policy && npm run build"

# 6. Expose port 8080, deny outbound, and start the server
arker update "$VM" --policies-file /dev/stdin >/dev/null <<'JSON'
{
  "rules": {
    "npm": null,
    "no-egress": { "type": "outbound", "priority": 100, "action": "deny" },
    "app": {
      "type": "inbound",
      "priority": 100,
      "match": {
        "ports": [8080]
      },
      "action": "allow"
    }
  }
}
JSON

SESSION=$(arker sessions create "$VM" --cwd /workspace/react-policy | jq -r .session_id)
arker run --session-id "$SESSION" --time-to-background 0 --timeout 0 \
  "$VM" "exec node server.mjs"

echo "https://$VM-8080.aws-$ARKER_REGION.arker.app"

# Delete the VM when you are finished
# arker rm "$VM"`;

export default function App() {
  return (
    <main>
      <h1>Hello from an Arker VM!</h1>

      <p className="intro">
        This React project was synced from your computer to an Arker VM, built there, and exposed with Arker policies.
      </p>

      <h2>Run it from the CLI</h2>
      <p className="section-copy">
        Here is the CLI equivalent for the quickstart script that did this in Python:
      </p>

      <section className="command-panel" aria-label="CLI commands">
        <div className="command-panel__header">bash</div>
        <pre>
          <code>{cliCommands}</code>
        </pre>
      </section>

      <a href="https://arker.ai/docs/api/policies">Read the policy docs</a>
    </main>
  );
}
