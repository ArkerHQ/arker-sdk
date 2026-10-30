"""Owned pinned source with Daytona's non-root UID for mount comparisons."""

import uuid

from e2b_baseline import E2B_AMD64_IMAGE

PREFIX = "daytona-baseline-"


def create_daytona_baseline(client, *, platform):
    marker = PREFIX + uuid.uuid4().hex
    vm = client.fork(image=E2B_AMD64_IMAGE, platforms=[platform], name=marker)
    try:
        if vm.refresh().name != marker:
            raise RuntimeError("Owned Daytona baseline marker mismatch")
        command = """set -eu
command -v python3
command -v setpriv
test "$(id -u)" = 0
! getent passwd 1000
! getent group 1000
! getent passwd daytona
groupadd --gid 1000 daytona
useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash daytona
test "$(stat -c '%u:%g' /home/daytona)" = 1000:1000
setpriv --reuid=daytona --regid=daytona --init-groups -- test -w /home/daytona
"""
        result = vm.run(command, timeout=60)
        if result.exit_code:
            raise RuntimeError("Owned Daytona baseline preparation failed")
        return vm
    except BaseException as primary:
        try:
            delete_daytona_baseline(vm, marker)
        except BaseException as cleanup:
            raise cleanup from primary
        raise


def delete_daytona_baseline(vm, marker):
    if not marker.startswith(PREFIX) or vm.refresh().name != marker:
        raise RuntimeError("Refusing to delete an unproven Daytona baseline")
    vm.delete()
