from __future__ import annotations

import math
import re
import shlex
from functools import partial
from typing import Any

from ._compat import (
    CommandResult,
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
        self.sandbox_id = vm.id if vm is not None else getattr(native, "sandbox_id", getattr(native, "id", None))
        self.sandboxId = self.sandbox_id

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
                raise RuntimeError("Native Modal SDK is not installed")
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
    def from_id(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
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
                    return get_or_none(partial(method, sandbox_id))
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

    def exec(self, *command: Any, **kwargs: Any) -> Process:
        workdir = kwargs.pop("workdir", kwargs.pop("cwd", None))
        environment = kwargs.pop("env", None)
        timeout = kwargs.pop("timeout", None)
        timeout_ms = kwargs.pop("timeout_ms", kwargs.pop("timeoutMs", None))
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.exec received unsupported option(s): {unsupported}")
        if self._vm is not None:
            prefix = ""
            if environment:
                exports = []
                for key, value in environment.items():
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)) is None:
                        raise ValueError(f"Invalid environment variable name: {key}")
                    exports.append(f"export {key}={shlex.quote(str(value))};")
                prefix += " ".join(exports) + " "
            prefix += f"cd {shlex.quote(str(workdir or '/'))} && "
            arker_timeout = timeout if timeout is not None else math.ceil(timeout_ms / 1_000) if timeout_ms else None
            return Process(run_command(self._vm, prefix + shell_command(command), timeout=arker_timeout))
        native_kwargs = {
            **({"workdir": workdir} if workdir is not None else {}),
            **({"env": environment} if environment is not None else {}),
            **({"timeout": timeout} if timeout is not None else {}),
            **({"timeout_ms": timeout_ms} if timeout_ms is not None else {}),
        }
        return Process(self._native.exec(*command, **native_kwargs))

    def terminate(self) -> None:
        if self._vm is not None:
            self._vm.delete()
            return
        if hasattr(self._native, "terminate"):
            self._native.terminate()
            return
        if hasattr(self._native, "close"):
            self._native.close()
            return
        raise AttributeError("Native Modal sandbox does not expose terminate().")
