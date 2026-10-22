from __future__ import annotations

import dataclasses
import json
import math
import os
import random
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any


def is_arker_vm_identifier(identifier: str) -> bool:
    """Recognize Arker-shaped identifiers, without establishing ownership."""
    return (
        re.fullmatch(r"(?:vmh-[0-9a-f]{16}-)?[0-7][0-9a-hjkmnp-tv-z]{25}", identifier, re.IGNORECASE | re.ASCII)
        is not None
    )


def provider_accepts_identifier(provider: str, identifier: str, *, arker_enabled: bool) -> bool:
    # E2B and Modal reject Arker's ID grammar rather than returning absence.
    return not (arker_enabled and provider in {"e2b", "modal"} and is_arker_vm_identifier(identifier))


@dataclasses.dataclass(frozen=True)
class ExistingRoute:
    strategy: str
    order: tuple[str, ...]
    timeout_ms: int = 30_000


class RouterLookupTimeoutError(TimeoutError):
    """The ownership lookup budget expired; this does not establish absence."""


@dataclasses.dataclass(frozen=True)
class CreateRoute:
    strategy: str
    order: tuple[str, ...] = ()
    weights: dict[str, float] = dataclasses.field(default_factory=dict)
    fallback: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class CacheConfig:
    path: Path | None = None
    max_bytes: int = 4 * 1024 * 1024
    max_entries: int = 20_000


@dataclasses.dataclass(frozen=True)
class RouterConfig:
    version: int
    existing: ExistingRoute
    create: CreateRoute
    cache: CacheConfig = CacheConfig()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RouterConfig:
        unknown = set(raw) - {"version", "existing", "create", "cache"}
        if unknown:
            raise ValueError(f"router config has unknown field {sorted(unknown)[0]}")
        if type(raw.get("version")) is not int or raw.get("version") != 1:
            raise ValueError(f"unsupported router config version {raw.get('version')}")
        existing_raw = _mapping(raw.get("existing"), "existing")
        _reject_unknown(existing_raw, {"strategy", "order", "timeoutMs"}, "existing")
        existing_strategy = existing_raw.get("strategy")
        if existing_strategy not in {"race", "priority"}:
            raise ValueError("existing.strategy must be race or priority")
        timeout_ms = existing_raw.get("timeoutMs", 30_000)
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 9_007_199_254_740_991:
            raise ValueError("existing.timeoutMs must be a positive safe integer")
        existing = ExistingRoute(existing_strategy, _names(existing_raw.get("order"), "existing.order"), timeout_ms)

        create_raw = _mapping(raw.get("create"), "create")
        create_strategy = create_raw.get("strategy")
        if create_strategy == "priority":
            _reject_unknown(create_raw, {"strategy", "order"}, "create")
            create = CreateRoute("priority", order=_names(create_raw.get("order"), "create.order"))
        elif create_strategy == "weighted":
            _reject_unknown(create_raw, {"strategy", "weights", "fallback"}, "create")
            weights_raw = _mapping(create_raw.get("weights"), "create.weights")
            weights: dict[str, float] = {}
            for provider, value in weights_raw.items():
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError(f"weight for {provider} must be a positive finite number")
                weights[provider] = float(value)
            if not weights:
                raise ValueError("create.weights must not be empty")
            fallback = _names(create_raw.get("fallback", list(weights)), "create.fallback")
            create = CreateRoute("weighted", weights=weights, fallback=fallback)
        else:
            raise ValueError("create.strategy must be priority or weighted")

        cache_raw = _mapping(raw.get("cache", {}), "cache")
        _reject_unknown(cache_raw, {"path", "maxBytes", "maxEntries"}, "cache")
        cache_path = cache_raw.get("path")
        if cache_path is not None and (not isinstance(cache_path, str) or not cache_path):
            raise ValueError("cache.path must be a non-empty string")
        max_bytes = cache_raw.get("maxBytes", 4 * 1024 * 1024)
        max_entries = cache_raw.get("maxEntries", 20_000)
        for label, value in (("maxBytes", max_bytes), ("maxEntries", max_entries)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"cache.{label} must be a positive integer")
        cache = CacheConfig(
            path=Path(cache_path) if cache_path is not None else None,
            max_bytes=max_bytes,
            max_entries=max_entries,
        )
        return cls(1, existing, create, cache)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown field {sorted(unknown)[0]}")


def _names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{label} must be a non-empty list of provider names")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate providers")
    return tuple(value)


def load_router_config(path: str | Path = "computesdk_router.json") -> RouterConfig:
    return RouterConfig.from_dict(json.loads(Path(path).read_text()))


def select_weighted_provider(weights: Mapping[str, float], *, random_value: float | None = None) -> str:
    sample = random.random() if random_value is None else random_value
    scale = max(weights.values())
    scaled = {provider: weight / scale for provider, weight in weights.items()}
    point = min(max(sample, 0.0), math.nextafter(1.0, 0.0)) * sum(scaled.values())
    for provider, weight in scaled.items():
        if point < weight:
            return provider
        point -= weight
    return next(reversed(weights))


class JsonRouteCache:
    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = 20_000,
        max_bytes: int = 4 * 1024 * 1024,
        now: Callable[[], int | float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.now = now or (lambda: int(time.time() * 1000))

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        descriptor: int | None = None
        token = secrets.token_hex(16)
        for attempt in range(101):
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(descriptor, token.encode())
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 30:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if attempt == 100:
                    raise TimeoutError(f"timed out acquiring route cache lock {lock_path}") from None
                time.sleep(min(0.002 + attempt / 1000, 0.025))
        if descriptor is None:
            raise TimeoutError(f"timed out acquiring route cache lock {lock_path}")
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                if lock_path.read_text() == token:
                    lock_path.unlink()
            except FileNotFoundError:
                pass

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or type(payload.get("version")) is not int
                or payload["version"] != 1
                or not isinstance(payload.get("entries"), dict)
            ):
                raise ValueError
            payload["entries"] = {
                identifier: entry
                for identifier, entry in payload["entries"].items()
                if isinstance(identifier, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("provider"), str)
                and isinstance(entry.get("accessedAt"), (int, float))
                and not isinstance(entry.get("accessedAt"), bool)
                and math.isfinite(entry["accessedAt"])
            }
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"version": 1, "entries": {}}

    def _write(self, payload: dict[str, Any]) -> None:
        ordered = sorted(payload["entries"].items(), key=lambda item: item[1]["accessedAt"])
        while len(ordered) > self.max_entries:
            identifier, _ = ordered.pop(0)
            payload["entries"].pop(identifier, None)
        while ordered and len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()) > self.max_bytes:
            identifier, _ = ordered.pop(0)
            payload["entries"].pop(identifier, None)
        if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()) > self.max_bytes:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def _next_access(self, payload):
        latest = max((entry["accessedAt"] for entry in payload["entries"].values()), default=-1)
        return max(self.now(), latest + 1)

    def get(self, identifier: str) -> str | None:
        with self._locked():
            payload = self._read()
            entry = payload["entries"].get(identifier)
            if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str):
                return None
            entry["accessedAt"] = self._next_access(payload)
            self._write(payload)
            return entry["provider"]

    def set(self, identifier: str, provider: str) -> None:
        with self._locked():
            payload = self._read()
            payload["entries"][identifier] = {"provider": provider, "accessedAt": self._next_access(payload)}
            self._write(payload)

    def delete(self, identifier: str) -> None:
        with self._locked():
            payload = self._read()
            payload["entries"].pop(identifier, None)
            self._write(payload)
