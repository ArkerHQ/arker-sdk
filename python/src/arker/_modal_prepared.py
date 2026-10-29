"""Canonical nonsecret metadata for an owned prepared-image builder."""

import json
import re
import shlex
from collections.abc import Mapping, Sequence

CONFIG_PATH = "/etc/arker/image-config.json"
PROFILE_PATH = "/etc/profile.d/99-arker-image.sh"


def image_metadata(
    *,
    env: Mapping[str, str],
    user: str | None,
    workdir: str | None,
    entrypoint: Sequence[str] | None,
    cmd: Sequence[str] | None,
) -> tuple[bytes, bytes]:
    """Encode fully resolved metadata; callers resolve inheritance before this step."""

    def text(value):
        if not isinstance(value, str) or "\0" in value or len(value.encode()) > 32768:
            raise ValueError("Invalid prepared image configuration")
        return value

    def argv(values):
        if values is None:
            return None
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError("Prepared image commands require argument sequences")
        return text(" ".join(shlex.quote(text(value)) for value in values)) or None

    if not isinstance(env, Mapping) or len(env) > 256:
        raise ValueError("Invalid prepared image environment")
    pairs = []
    for key, value in env.items():
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError("Invalid prepared image environment name")
        pairs.append([key, text(value)])
    if user is not None and (not text(user) or re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", user) is None):
        raise ValueError("Invalid prepared image user")
    if workdir is not None and not text(workdir).startswith("/"):
        raise ValueError("Prepared image workdir must be absolute")
    config = json.dumps(
        {"env": pairs, "user": user, "workdir": workdir, "entrypoint": argv(entrypoint), "cmd": argv(cmd)},
        ensure_ascii=False,
    ).encode()
    if len(config) > 1024 * 1024:
        raise ValueError("Prepared image configuration exceeds 1 MiB")
    profile = "# Resolved image environment and working directory.\n"
    profile += "".join(f"export {key}={shlex.quote(value)}\n" for key, value in pairs)
    if workdir is not None:
        profile += f"cd {shlex.quote(workdir)} 2>/dev/null || true\n"
    return config, profile.encode()


BUILD_POLICY = "computesdk-modal-build-lifetime"
IMAGE_WORKDIR = "computesdk-modal-image-workdir"


def finish_prepared(
    builder,
    *,
    encoded_metadata,
    final_policies,
    checkpoint_timeout=300,
    cancelled=None,
    final_metadata=None,
    final_name=None,
):
    """Take ownership of a settled builder and return its started disk-only child.

    The caller installs BUILD_POLICY when creating the builder. Final policies
    include the independently validated sandbox lifetime, never build time.
    """
    import asyncio
    import builtins
    from copy import deepcopy

    from .computer import VM, ArkerError

    owned = [builder]

    def check_cancelled():
        if cancelled is not None and cancelled.is_set():
            raise asyncio.CancelledError

    def fork(source, **options):
        check_cancelled()
        try:
            result = source.fork(**options)
        except ArkerError as error:
            recovery = getattr(error.body, "recovery", None)
            context = recovery.context if recovery is not None else None
            if context is not None and context.vm_id and all(vm.id != str(context.vm_id) for vm in owned):
                owned.append(VM(builder._client, str(context.vm_id), builder.base_url))
            raise
        owned.append(result)
        check_cancelled()
        return result

    def clean(resource):
        try:
            resource.delete()
        except Exception as error:
            error.vm_id = resource.id
            raise

    try:
        if type(checkpoint_timeout) is not int or checkpoint_timeout <= 0:
            raise ValueError("Prepared capture requires a positive retention timeout")
        policies = deepcopy(final_policies)
        if "policies" in policies:
            raise ValueError("Prepared images require named policies")
        if BUILD_POLICY in policies.get("rules", {}):
            raise ValueError("Prepared build lifetime policy is reserved")
        policies.setdefault("rules", {})[BUILD_POLICY] = None
        config, profile = encoded_metadata
        check_cancelled()
        builder.sync(PROFILE_PATH, profile)
        check_cancelled()
        builder.sync(CONFIG_PATH, config)
        capture = fork(
            builder,
            immutable=True,
            policies={
                "rules": {
                    BUILD_POLICY: {"type": "schedule", "after_created": checkpoint_timeout, "action": "delete"},
                }
            },
        )
        child_options = {"layers": ["disk"], "start_image": True, "policies": policies}
        if final_metadata is not None:
            child_options["metadata"] = final_metadata
        if final_name is not None:
            child_options["name"] = final_name
        child = fork(capture, **child_options)
        # Keep the child owned until every intermediate has been reclaimed.
        for resource in list(reversed(owned[:-1])):
            clean(resource)
            owned.remove(resource)
        check_cancelled()
        return child
    except BaseException as primary:
        failures = []
        for resource in reversed(owned):
            try:
                clean(resource)
            except Exception as error:
                failures.append(error)
        if failures:
            raise builtins.BaseExceptionGroup(
                "Prepared image creation and cleanup failed", [primary, *failures]
            ) from primary
        raise


def recipe_steps(commands):
    """Validate the initial registry recipe subset before allocating a builder."""
    from ._compat import UnsupportedCompatibilityOption
    from .build_spec import Cmd, Entrypoint, Run, parse_dockerfile

    if not isinstance(commands, list) or not all(isinstance(command, str) for command in commands):
        raise TypeError("Image setup commands must be a list of strings")
    steps = parse_dockerfile('FROM prepared-base\nSHELL ["/bin/sh", "-c"]\n' + "\n".join(commands)).steps
    if any(not isinstance(step, (Run, Cmd, Entrypoint)) for step in steps):
        raise UnsupportedCompatibilityOption("Prepared registry recipes currently support RUN, CMD and ENTRYPOINT")
    if not any(isinstance(step, Cmd) for step in steps) or not any(isinstance(step, Entrypoint) for step in steps):
        raise UnsupportedCompatibilityOption("Prepared recipes must explicitly set both CMD and ENTRYPOINT")
    return steps


def create_prepared(client, request, steps, *, build_timeout=900):
    """Build on a private temporary VM; apply the sandbox lifetime only to its child."""
    import builtins
    import time
    from copy import deepcopy

    from ._compat import UnsupportedCompatibilityOption
    from .build_spec import Cmd, Entrypoint, Run

    policies = deepcopy(request.get("policies") or {})
    rules = policies.get("rules", {})
    if "policies" in policies or policies.get("secrets") or BUILD_POLICY in rules:
        raise UnsupportedCompatibilityOption(
            "Prepared builds require named policies without policy secrets or reserved build rules"
        )
    ingress = "computesdk-modal-build-ingress"
    if (
        any(rule and rule.get("type") == "inbound" for rule in rules.values())
        or ingress in rules
        or "computesdk-modal-prepared-ingress" in rules
    ):
        raise UnsupportedCompatibilityOption("Prepared builds cannot yet preserve custom inbound rules")
    if request.get("image_config") is not None:
        raise UnsupportedCompatibilityOption("Prepared recipes cannot combine an explicit image_config override")
    if type(build_timeout) is not int or build_timeout <= 0:
        raise ValueError("Prepared image build timeout must be positive")
    build_rules = {key: value for key, value in rules.items() if key != "computesdk-modal-lifetime"}
    build_rules[BUILD_POLICY] = {"type": "schedule", "after_created": build_timeout, "action": "delete"}
    build_rules[ingress] = {"type": "inbound", "priority": 0, "action": "deny"}
    initial = {key: value for key, value in request.items() if key not in ("name", "metadata", "policies")}
    initial.update(image_config={"cmd": [], "entrypoint": []}, policies={**policies, "rules": build_rules})
    deadline = time.monotonic() + build_timeout
    try:
        builder = client.fork(**initial)
    except Exception as primary:
        from .computer import VM, ArkerError

        recovery = getattr(primary.body, "recovery", None) if isinstance(primary, ArkerError) else None
        context = recovery.context if recovery is not None else None
        if context is not None and context.vm_id:
            resource = VM(client, str(context.vm_id))
            try:
                resource.delete()
            except Exception as cleanup:
                cleanup.vm_id = resource.id
                raise builtins.ExceptionGroup("Prepared allocation and cleanup failed", [primary, cleanup]) from primary
        raise
    transferred = False
    try:
        raw = builder.sync(CONFIG_PATH)
        if len(raw) > 1024 * 1024:
            raise ValueError("Prepared image configuration exceeds 1 MiB")
        config = json.loads(raw)
        resolved = {
            "env": dict(config["env"]),
            "user": config.get("user"),
            "workdir": config.get("workdir"),
            "cmd": None,
            "entrypoint": None,
        }
        image_metadata(**resolved)
        for step in steps:
            if isinstance(step, Run):
                remaining = int(deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("Prepared image build deadline exceeded")
                session = builder.create_session()
                try:
                    result = builder.run(step.command, session_id=session.session_id, timeout=remaining)
                    if result.exit_code != 0:
                        raise RuntimeError("Prepared image build command failed")
                except BaseException as primary:
                    try:
                        builder.delete_session(session.session_id)
                    except Exception as cleanup:
                        raise builtins.BaseExceptionGroup(
                            "Build command and session cleanup failed", [primary, cleanup]
                        ) from primary
                    raise
                else:
                    builder.delete_session(session.session_id)
            elif isinstance(step, (Cmd, Entrypoint)):
                resolved["cmd" if isinstance(step, Cmd) else "entrypoint"] = shlex.split(step.command)
        encoded = image_metadata(**resolved)
        policies.setdefault("rules", {})[ingress] = None
        policies["rules"]["computesdk-modal-prepared-ingress"] = {
            "type": "inbound",
            "priority": 0,
            "action": "allow",
            "auth": "arker",
        }
        transferred = True
        return finish_prepared(
            builder,
            encoded_metadata=encoded,
            final_policies=policies,
            final_metadata={**(request.get("metadata") or {}), IMAGE_WORKDIR: resolved["workdir"] or "/"},
            final_name=request.get("name"),
        )
    except BaseException as primary:
        if not transferred:
            try:
                builder.delete()
            except Exception as cleanup:
                cleanup.vm_id = builder.id
                raise builtins.BaseExceptionGroup("Prepared build and cleanup failed", [primary, cleanup]) from primary
        raise
