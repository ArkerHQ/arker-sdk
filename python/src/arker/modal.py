from __future__ import annotations

import math
import re
import shlex
import time
from functools import partial
from typing import Any

from ._compat import (
    CommandResult,
    CompatibilityCreationNotStartedError,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    arker_create,
    arker_get,
    compat_network_policies,
    format_not_found,
    get_or_none,
    import_optional,
    routed_create,
    routed_get,
    run_command,
    shell_command,
)
from .computer import VM


def __getattr__(name: str) -> Any:
    module = import_optional("modal")
    if module is not None:
        return getattr(module, name)
    raise AttributeError(name)


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text

    def read_text(self) -> str:
        return self._text

    def readText(self) -> str:
        return self._text


class Process:
    def __init__(self, result: CommandResult | Any) -> None:
        if isinstance(result, CommandResult):
            self._exit_code = result.exit_code
            self.stdout = _Stream(result.stdout)
            self.stderr = _Stream(result.stderr)
        else:
            self._native = result
            self.stdout = result.stdout
            self.stderr = result.stderr
            self._exit_code = None

    def wait(self) -> int:
        native = getattr(self, "_native", None)
        if native is not None and hasattr(native, "wait"):
            return native.wait()
        return int(self._exit_code)


class Sandbox(UnsupportedMixin):
    _compat_name = "modal"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self._processes = None
        self._filesystem_container = None
        self._filesystem = None
        self.sandbox_id = (
            vm.id
            if vm is not None
            else getattr(native, "object_id", getattr(native, "sandbox_id", getattr(native, "id", None)))
        )
        self.sandboxId = self.sandbox_id

    @property
    def filesystem(self):
        if self._native is not None:
            return self._native.filesystem
        if self._filesystem is None:
            from ._modal_filesystem import FilesystemContainer, filesystem

            self._filesystem_container = FilesystemContainer(self)
            self._filesystem = filesystem(self._filesystem_container)
        return self._filesystem

    @property
    def object_id(self) -> str:
        return self.sandbox_id

    @staticmethod
    def _native_sandbox() -> Any | None:
        module = import_optional("modal")
        return getattr(module, "Sandbox", None) if module is not None else None

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Sandbox:
        native_kwargs = dict(kwargs)
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        native_kwargs.pop("arker", None)
        native_kwargs.pop("router_config_path", None)
        native_kwargs.pop("routerConfigPath", None)
        block_network = kwargs.pop("block_network", kwargs.pop("blockNetwork", False))
        outbound_cidrs = kwargs.pop("outbound_cidr_allowlist", kwargs.pop("outboundCidrAllowlist", None))
        outbound_domains = kwargs.pop("outbound_domain_allowlist", kwargs.pop("outboundDomainAllowlist", None))
        for camel, snake, value in (
            ("blockNetwork", "block_network", block_network),
            ("outboundCidrAllowlist", "outbound_cidr_allowlist", outbound_cidrs),
            ("outboundDomainAllowlist", "outbound_domain_allowlist", outbound_domains),
        ):
            if camel in native_kwargs:
                native_kwargs.pop(camel)
                native_kwargs[snake] = value
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        native = cls._native_sandbox()

        def create_native() -> Any:
            if native is None or not hasattr(native, "create"):
                raise CompatibilityCreationNotStartedError("Native Modal SDK is not installed")
            return native.create(*args, **native_kwargs)

        def create_arker() -> VM:
            if args or kwargs:
                names = [*(f"positional[{index}]" for index in range(len(args))), *sorted(kwargs)]
                raise UnsupportedCompatibilityOption(
                    f"Sandbox.create option(s) are not supported by Arker: {', '.join(names)}"
                )
            return arker_create(
                arker_config,
                policies=compat_network_policies(
                    block_all=bool(block_network),
                    domains=outbound_domains,
                    cidrs=outbound_cidrs,
                    domain_ports=[443],
                    empty_allowlist_blocks=True,
                ),
            )

        provider, result = routed_create(
            "modal",
            arker_config,
            {
                "arker": create_arker,
                "modal": create_native,
            },
        )
        return cls(vm=result if provider == "arker" else None, native=result if provider == "modal" else None)

    @classmethod
    def from_id(cls, sandbox_id: str, client: Any | None = None, **kwargs: Any) -> Sandbox:
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.from_id received unsupported option(s): {unsupported}")
        native = cls._native_sandbox()

        def get_native() -> Any | None:
            for method_name in ("from_id", "fromId", "connect"):
                if native is not None and hasattr(native, method_name):
                    method = getattr(native, method_name)
                    return get_or_none(
                        partial(method, sandbox_id, **({"client": client} if client is not None else {}))
                    )
            return None

        found = routed_get(
            sandbox_id,
            "modal",
            arker_config,
            {"arker": lambda: arker_get(sandbox_id, arker_config), "modal": get_native},
        )
        if found is not None:
            provider, result = found
            return cls(vm=result if provider == "arker" else None, native=result if provider == "modal" else None)
        raise format_not_found("Sandbox", sandbox_id)

    @classmethod
    def fromId(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
        return cls.from_id(sandbox_id, **kwargs)

    def exec(self, *command: Any, **kwargs: Any) -> Any:
        if self._vm is None:
            return self._native.exec(*command, **kwargs)
        workdir = kwargs.pop("workdir", kwargs.pop("cwd", None))
        environment = kwargs.pop("env", None)
        timeout = kwargs.pop("timeout", None)
        timeout_ms = kwargs.pop("timeout_ms", kwargs.pop("timeoutMs", None))
        native_streams = import_optional("modal.container_process") is not None
        stream_options = {}
        if native_streams:
            from modal._utils.async_utils import synchronizer
            from modal.exception import InvalidError
            from modal.sandbox._common import _validate_exec_args
            from modal.secret import _local_secret_env, _resolvable_secrets
            from modal.stream_type import StreamType

            _validate_exec_args(command)
            if workdir is not None and not workdir.startswith("/"):
                raise InvalidError(f"workdir must be an absolute path, got: {workdir}")
            stream_options = {key: kwargs.pop(key) for key in ("stdout", "stderr", "text", "bufsize") if key in kwargs}
            for key in ("stdout", "stderr"):
                if key in stream_options and stream_options[key] not in tuple(StreamType):
                    raise ValueError(f"Unsupported StreamType for {key}")
            if stream_options.get("bufsize") == 1 and stream_options.get("text", True) is False:
                raise ValueError("line-buffering is only supported when text=True")
            secrets = [synchronizer._translate_in(secret) for secret in (kwargs.pop("secrets", None) or [])]
            if _resolvable_secrets(secrets):
                raise UnsupportedCompatibilityOption(
                    "Sandbox.exec named or deployed secrets are not supported by Arker"
                )
            environment = {
                **_local_secret_env(secrets),
                **{k: v for k, v in (environment or {}).items() if v is not None},
            }
            for key, default in (("pty", False), ("pty_info", None), ("_pty_info", None)):
                if key in kwargs and kwargs[key] == default:
                    kwargs.pop(key)
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.exec received unsupported option(s): {unsupported}")
        prefix = ""
        if environment:
            exports = []
            for key, value in environment.items():
                if value is None:
                    continue
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)) is None:
                    raise ValueError(f"Invalid environment variable name: {key}")
                exports.append(f"export {key}={shlex.quote(str(value))};")
            prefix += " ".join(exports) + " "
        prefix += f"cd {shlex.quote(str(workdir or '/'))} && "
        arker_timeout = timeout if timeout is not None else math.ceil(timeout_ms / 1_000) if timeout_ms else None
        if native_streams:
            from ._compat_process import GuestProcesses
            from ._modal_process import modal_process

            if self._processes is None:
                self._processes = GuestProcesses(self._vm)
            milliseconds = timeout * 1000 if timeout is not None else timeout_ms
            deadline = time.monotonic() + milliseconds / 1000 if milliseconds else None
            current = self._processes.exec(
                {
                    "command": " ".join(shlex.quote(part) for part in command),
                    "argv": list(command),
                    "env": {key: value for key, value in (environment or {}).items() if value is not None},
                    "spawnFailureExitCode": 128,
                    "preserveDescendants": True,
                    "workingDir": workdir or "/",
                    "stdin": True,
                    **({"timeout": milliseconds} if milliseconds is not None else {}),
                }
            )
            return modal_process(
                self._processes,
                current.pid,
                text=stream_options.get("text", True),
                by_line=stream_options.get("bufsize") == 1,
                stdout=stream_options.get("stdout"),
                stderr=stream_options.get("stderr"),
                deadline=deadline,
            )
        return Process(run_command(self._vm, prefix + shell_command(command), timeout=arker_timeout))

    def terminate(self, *, wait: bool = False) -> int | None:
        if self._vm is not None:
            if wait:
                raise UnsupportedCompatibilityOption("Sandbox.terminate(wait=True) is not supported by Arker")
            self._vm.delete()
            return None
        if hasattr(self._native, "terminate"):
            return self._native.terminate(wait=wait)
        if hasattr(self._native, "close"):
            self._native.close()
            return None
        raise AttributeError("Native Modal sandbox does not expose terminate().")
