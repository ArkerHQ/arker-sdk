#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["arker"]
# ///
"""Build a local React app in an Arker VM and expose it with a policy.

Run from the repository root:

    ARKER_API_KEY=ark_live_... ./examples/react-policy/quick_start.py
"""

import os
from pathlib import Path
from urllib.request import urlopen

from arker import Arker

REGION = "us-west-2"
PROVIDER = "aws"
APP_PORT = 8080
LOCAL_APP = Path(__file__).parent / "app"
REMOTE_APP = "/workspace/react-policy"

BUILD_POLICY = {
    "policies": [
        {
            "type": "outbound",
            "match": {
                "hosts": ["registry.npmjs.org"],
                "ports": [443],
            },
            "action": "allow",
        }
    ]
}

PUBLIC_POLICY = {
    "policies": [
        {
            "type": "inbound",
            "match": {"ports": [APP_PORT]},
            "action": "allow",
            "auth": "open",
        }
    ]
}


def main() -> None:
    source_vm = os.environ["ARKER_SOURCE_VM"]
    arker = Arker(region=REGION, provider=PROVIDER)
    vm = None

    try:
        print(f"1. Forking {source_vm}")
        print("   Build policy: allow registry.npmjs.org:443")
        print('   $ arker fork "$ARKER_SOURCE_VM"')
        print("   $ arker policies set $VM  # allow registry.npmjs.org:443")
        vm = arker.fork(
            source_vm_name=source_vm,
            name="react-policy-demo",
            policies=BUILD_POLICY,
        )
        print(f"   VM: {vm.id}")

        print(f"\n2. Syncing ./app to {REMOTE_APP}")
        print(
            "   $ tar --exclude=node_modules --exclude=dist -C ./app -czf - ."
            " | arker sync $VM /tmp/react-policy.tgz"
        )
        print(
            f'   $ arker run $VM "mkdir -p {REMOTE_APP}'
            f' && tar -xzf /tmp/react-policy.tgz -C {REMOTE_APP}"'
        )
        vm.sync_dir(str(LOCAL_APP), REMOTE_APP)

        print("\n3. Building the React project on the VM")
        print(
            f'   $ arker run $VM "cd {REMOTE_APP}'
            ' && npm ci --include=dev --no-audit --no-fund"'
        )
        print(f'   $ arker run $VM "cd {REMOTE_APP} && npm run build"')
        build = vm.run(
            f"""
            set -eu
            cd {REMOTE_APP}
            npm ci --include=dev --no-audit --no-fund
            npm run build
            """
        )
        if build.exit_code != 0:
            raise RuntimeError(build.stderr.strip())

        print(f"\n4. Exposing the app on port {APP_PORT}")
        print(f"   Runtime policy: allow public inbound traffic on :{APP_PORT}")
        print(f"   $ arker policies set $VM  # allow public inbound on :{APP_PORT}")
        print(f"   $ arker sessions create $VM --cwd {REMOTE_APP}")
        print(
            "   $ arker run --session-id $SESSION --time-to-background 0 --timeout 0"
            ' $VM "exec node server.mjs"'
        )
        session = vm.create_session(cwd=REMOTE_APP)
        server = vm.run(
            "exec node server.mjs",
            session_id=session.session_id,
            time_to_background=0,
            timeout=0,
            policies=PUBLIC_POLICY,
        )

        # Wait until the server accepts connections before checking it publicly.
        health = vm.run(
            "for attempt in $(seq 1 10); do "
            f"node -e \"fetch('http://127.0.0.1:{APP_PORT}/healthz')"
            ".then(async response => process.exit("
            "response.ok && await response.text() === 'ok\\n' ? 0 : 1))"
            ".catch(() => process.exit(1))\" && exit 0; "
            "sleep 1; done; exit 1"
        )
        if health.exit_code != 0:
            raise RuntimeError(health.stderr.strip())

        public_url = f"https://{vm.id}-{APP_PORT}.{PROVIDER}-{REGION}.arker.app"
        print(f"\n5. Verifying {public_url}")
        print("   $ curl $APP_URL/healthz")
        with urlopen(f"{public_url}/healthz", timeout=10) as response:
            if response.read() != b"ok\n":
                raise RuntimeError(
                    "The public health check returned unexpected content"
                )
        with urlopen(f"{public_url}/", timeout=10) as response:
            if b"<title>Arker React policy demo</title>" not in response.read():
                raise RuntimeError("The public app returned unexpected content")

        # Denied connections are dropped silently, so the probe bounds its
        # own connect timeout.
        print("\n6. Verifying egress is now locked down")
        print('   $ arker run $VM "node -e fetch(https://registry.npmjs.org)"')
        lockdown = vm.run(
            "node -e \"fetch('https://registry.npmjs.org', "
            "{signal: AbortSignal.timeout(3000)})"
            ".then(() => process.exit(0), () => process.exit(1))\"",
            timeout=30,
        )
        if lockdown.exit_code == 0:
            raise RuntimeError("Outbound traffic is unexpectedly still allowed")
        print("   Blocked, as intended: the runtime policy has no outbound rule.")

    except BaseException:
        if vm is not None:
            print(f"\nSetup failed. Deleting {vm.id}...")
            vm.delete()
        raise

    print("\nYour React web app is now running on an Arker VM!\n")
    print(f"App URL: {public_url}")
    print(f"VM:      {vm.id}")
    print(f"Run:     {server.run_id}")
    print(f"Session: {session.session_id}")
    print(f"Delete:  arker rm {vm.id}")


if __name__ == "__main__":
    main()
