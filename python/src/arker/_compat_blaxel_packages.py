"""Host-side staging of the qualified Alpine package closure."""

import json
import shlex
from contextlib import contextmanager
from importlib.resources import files

from ._compat_e2b_artifact import stage_pinned_artifact

ROOT = "/root/.arker/blaxel"


def qualified_firewall_image(image):
    manifest = json.loads(files("arker").joinpath("_guest_blaxel_packages.json").read_text())
    return image if image == manifest["image"] else None


def validate_firewall_startup(image, configuration):
    if qualified_firewall_image(image) is not None and (
        not isinstance(configuration, dict) or configuration.get("entrypoint") != [] or configuration.get("cmd") != []
    ):
        raise ValueError("Qualified firewall image requires explicit empty entrypoint and cmd before creation")


@contextmanager
def staged_firewall_packages(vm, profile, cache_directory):
    manifest = json.loads(files("arker").joinpath("_guest_blaxel_packages.json").read_text())
    if profile != manifest["profile"]:
        raise ValueError("Firewall packages require the supported runtime profile")
    owned = []
    candidates = []
    failure = None
    try:
        for entry in manifest["packages"]:
            original = stage_pinned_artifact(vm, ROOT, entry, cache_directory)
            destination = original + ".apk"
            owned.extend([original, destination])
            result = vm.run(f"/bin/mv -- {shlex.quote(original)} {shlex.quote(destination)}", timeout=10)
            if result.exit_code != 0:
                raise RuntimeError("Firewall package staging rename failed")
            candidates.append(destination)
        yield candidates
    except BaseException as error:
        failure = error
        raise
    finally:
        if owned:
            try:
                result = vm.run("/bin/rm -f -- " + " ".join(map(shlex.quote, owned)), timeout=10)
                if result.exit_code != 0:
                    raise RuntimeError("Firewall package staging cleanup failed")
            except Exception as error:
                if failure is None:
                    raise
                failure.cleanup_error = error
