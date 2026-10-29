"""Local Modal recipes; provider-owned objects are realized only after routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._compat import UnsupportedCompatibilityOption, import_optional


@dataclass(frozen=True, repr=False)
class Image:
    _method: str
    _options: dict[str, Any] = field(repr=False)

    @staticmethod
    def from_registry(
        tag: str,
        secret: Any = None,
        *,
        setup_dockerfile_commands: list[str] = [],  # noqa: B006 - Native signature; copied below.
        force_build: bool = False,
        add_python: str | None = None,
        **kwargs: Any,
    ) -> Image:
        return Image(
            "from_registry",
            {
                "tag": tag,
                "secret": secret,
                "setup_dockerfile_commands": list(setup_dockerfile_commands),
                "force_build": force_build,
                "add_python": add_python,
                **kwargs,
            },
        )

    @staticmethod
    def debian_slim(python_version: str | None = None, force_build: bool = False) -> Image:
        return Image("debian_slim", {"python_version": python_version, "force_build": force_build})

    def _native(self) -> Any:
        module = import_optional("modal")
        if module is None:
            raise UnsupportedCompatibilityOption("Native Modal SDK is required to realize a Modal image")
        options = dict(self._options)
        if type(options.get("secret")) is Secret:
            options["secret"] = options["secret"]._native()
        return getattr(module.Image, self._method)(**options)

    def _registry_auth(self) -> dict[str, str] | None:
        secret = self._options.get("secret")
        if secret is None:
            return None
        if type(secret) is not Secret:
            raise UnsupportedCompatibilityOption("Registry authentication requires a local Secret.from_dict recipe")
        values = secret._values
        if any(not values.get(key) for key in ("REGISTRY_USERNAME", "REGISTRY_PASSWORD")):
            raise ValueError("Registry secret requires REGISTRY_USERNAME and REGISTRY_PASSWORD")
        return {"username": values["REGISTRY_USERNAME"], "password": values["REGISTRY_PASSWORD"]}

    def _registry(self) -> str:
        if self._method != "from_registry":
            raise UnsupportedCompatibilityOption(
                "Image.debian_slim requires Python and build-tool setup not yet supported by Arker"
            )
        allowed = {"tag", "secret", "setup_dockerfile_commands", "force_build", "add_python"}
        if (
            set(self._options) - allowed
            or self._options["setup_dockerfile_commands"]
            or self._options["force_build"] is not False
            or self._options["add_python"] is not None
        ):
            raise UnsupportedCompatibilityOption(
                "Arker supports plain Image.from_registry recipes; image build options are unsupported"
            )
        tag = self._options["tag"]
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError("Image.from_registry requires a nonempty registry reference")
        return tag


@dataclass(frozen=True, repr=False)
class App:
    _name: str
    _create_if_missing: bool
    _environment_name: str | None
    _client: Any = field(repr=False)

    @staticmethod
    def lookup(
        name: str, *, client: Any = None, environment_name: str | None = None, create_if_missing: bool = False
    ) -> App:
        return App(name, create_if_missing, environment_name, client)

    def _native(self) -> Any:
        module = import_optional("modal")
        if module is None:
            raise UnsupportedCompatibilityOption("Native Modal SDK is required to resolve a Modal app")
        return module.App.lookup(
            self._name,
            client=self._client,
            environment_name=self._environment_name,
            create_if_missing=self._create_if_missing,
        )

    def _group(self) -> dict[str, str]:
        if not self._create_if_missing or self._client is not None:
            raise UnsupportedCompatibilityOption(
                "Arker sandbox app grouping requires create_if_missing=True and no native client; "
                "existing Modal app ownership cannot be resolved by Arker"
            )
        if not isinstance(self._name, str) or not self._name:
            raise ValueError("App name must be a nonempty string")
        return {
            "name": self._name,
            **({"environment": self._environment_name} if self._environment_name is not None else {}),
        }


@dataclass(frozen=True, repr=False)
class Secret:
    _values: dict[str, str] = field(repr=False)

    @staticmethod
    def from_dict(env_dict: dict[str, str | None] = {}) -> Secret:  # noqa: B006 - Native signature; copied below.
        values = env_dict
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in values.items() if value is not None
        ):
            module = import_optional("modal.exception")
            error = module.InvalidError if module is not None else ValueError
            raise error("the env_dict argument to Secret has to be a dict[str, Union[str, None]]")
        return Secret({key: value for key, value in values.items() if value is not None})

    def _native(self) -> Any:
        module = import_optional("modal")
        if module is None:
            raise UnsupportedCompatibilityOption("Native Modal SDK is required to realize a Modal secret")
        return module.Secret.from_dict(dict(self._values))


def native_secrets(options):
    if not any(type(secret) is Secret for secret in (options.get("secrets") or [])):
        return options
    return {
        **options,
        "secrets": [secret._native() if type(secret) is Secret else secret for secret in (options["secrets"] or [])],
    }
