from __future__ import annotations

import json
import multiprocessing
import threading
import time
import types
from pathlib import Path

import pytest

from arker._compat import get_or_none, routed_create, routed_get, router_config
from arker._router import (
    JsonRouteCache,
    RouterConfig,
    is_arker_vm_identifier,
    load_router_config,
    provider_accepts_identifier,
    select_weighted_provider,
)


def _cache_writer(path: str, prefix: str) -> None:
    cache = JsonRouteCache(path, max_entries=1_000, max_bytes=1_000_000)
    for index in range(50):
        cache.set(f"{prefix}-{index}", "arker")


@pytest.fixture
def router_file(tmp_path: Path) -> Path:
    path = tmp_path / "computesdk_router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "race", "order": ["e2b", "arker"]},
                "create": {
                    "strategy": "weighted",
                    "weights": {"e2b": 90, "arker": 10},
                    "fallback": ["e2b", "arker"],
                },
                "cache": {"path": str(tmp_path / "routes.json"), "maxBytes": 4096, "maxEntries": 10},
            }
        )
    )
    return path


def test_loads_same_versioned_schema_as_typescript(router_file: Path) -> None:
    config = load_router_config(router_file)
    assert config == RouterConfig.from_dict(json.loads(router_file.read_text()))
    assert config.existing.order == ("e2b", "arker")
    assert config.create.weights == {"e2b": 90, "arker": 10}


def test_rejects_boolean_schema_version(router_file: Path) -> None:
    raw = json.loads(router_file.read_text())
    raw["version"] = True
    with pytest.raises(ValueError, match="unsupported router config version"):
        RouterConfig.from_dict(raw)


def test_large_weights_do_not_overflow() -> None:
    weights = {"blaxel": 1e308, "arker": 1e308}
    assert select_weighted_provider(weights, random_value=0.25) == "blaxel"
    assert select_weighted_provider(weights, random_value=0.75) == "arker"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_lookup_does_not_hide_http_failures_containing_not_found(status) -> None:
    error = RuntimeError("API key not found")
    error.status_code = status

    def fail():
        raise error

    with pytest.raises(RuntimeError, match="API key not found"):
        get_or_none(fail)


@pytest.mark.parametrize(
    "native,identifier",
    [
        (types.SimpleNamespace(metadata=types.SimpleNamespace(name="blaxel-name")), "blaxel-name"),
        (types.SimpleNamespace(object_id="modal-id"), "modal-id"),
    ],
)
def test_creation_caches_native_identifier_shapes(tmp_path, native, identifier):
    path = tmp_path / "router.json"
    cache_path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["blaxel", "arker"]},
                "create": {"strategy": "priority", "order": ["blaxel", "arker"]},
                "cache": {"path": str(cache_path)},
            }
        )
    )
    routed_create("blaxel", {"router_config_path": str(path)}, {"blaxel": lambda: native, "arker": lambda: None})
    assert JsonRouteCache(cache_path).get(identifier) == "blaxel"


def test_successful_creation_never_falls_through_on_metadata_failure(tmp_path):
    class Created:
        @property
        def id(self):
            raise RuntimeError("metadata unavailable")

    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["blaxel", "arker"]},
                "create": {"strategy": "priority", "order": ["blaxel", "arker"]},
            }
        )
    )
    created = Created()

    def duplicate():
        raise AssertionError("must not create another sandbox")

    provider, returned = routed_create(
        "blaxel", {"router_config_path": str(path)}, {"blaxel": lambda: created, "arker": duplicate}
    )
    assert provider == "blaxel"
    assert returned is created


def test_cache_lru_is_stable_when_clock_does_not_advance(tmp_path):
    cache = JsonRouteCache(tmp_path / "cache.json", max_entries=2, now=lambda: 0)
    cache.set("old", "arker")
    cache.set("cold", "blaxel")
    assert cache.get("old") == "arker"
    cache.set("new", "arker")
    assert cache.get("cold") is None
    assert cache.get("old") == "arker"


def test_default_router_uses_native_only_without_arker_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    config = router_config({}, "blaxel")
    assert config.existing.order == ("blaxel",)
    assert config.create.order == ("blaxel",)


def test_rejects_unknown_fields_and_credentials(tmp_path: Path) -> None:
    path = tmp_path / "computesdk_router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "race", "order": ["e2b"]},
                "create": {"strategy": "priority", "order": ["e2b"]},
                "apiKey": "must-not-be-accepted",
            }
        )
    )
    with pytest.raises(ValueError, match=r"unknown field.*apiKey"):
        load_router_config(path)


def test_rejects_unknown_nested_fields_and_invalid_cache_bounds() -> None:
    base = {
        "version": 1,
        "existing": {"strategy": "race", "order": ["e2b"]},
        "create": {"strategy": "priority", "order": ["e2b"]},
    }
    with pytest.raises(ValueError, match=r"unknown field token"):
        RouterConfig.from_dict({**base, "create": {**base["create"], "token": "secret"}})
    with pytest.raises(ValueError, match=r"positive integer"):
        RouterConfig.from_dict({**base, "cache": {"maxBytes": 0}})


@pytest.mark.parametrize(
    ("sample", "provider"),
    [(0.0, "e2b"), (0.899999, "e2b"), (0.9, "arker"), (0.999999, "arker")],
)
def test_weighted_selection_has_stable_boundaries(sample: float, provider: str) -> None:
    assert select_weighted_provider({"e2b": 90, "arker": 10}, random_value=sample) == provider


def test_json_cache_is_lru_bounded_and_cross_language_compatible(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    now = iter((100, 200, 300, 400, 500, 600, 700, 800))
    cache = JsonRouteCache(path, max_entries=2, max_bytes=4096, now=lambda: next(now))

    cache.set("old", "e2b")
    cache.set("hot", "arker")
    assert cache.get("old") == "e2b"
    cache.set("new", "modal")

    assert cache.get("old") == "e2b"
    assert cache.get("hot") is None
    assert cache.get("new") == "modal"
    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert set(payload["entries"]) == {"old", "new"}
    assert all(set(value) == {"provider", "accessedAt"} for value in payload["entries"].values())


def test_json_cache_recovers_from_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    path.write_text('{"version":1,"entries":')
    cache = JsonRouteCache(path, max_entries=10, max_bytes=4096)

    assert cache.get("missing") is None
    cache.set("vm-a", "arker")
    assert cache.get("vm-a") == "arker"


def test_json_cache_discards_malformed_entries_individually(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "valid": {"provider": "arker", "accessedAt": 1},
                    "broken": {"provider": "e2b", "accessedAt": "yesterday"},
                },
            }
        )
    )
    cache = JsonRouteCache(path)
    assert cache.get("valid") == "arker"
    assert cache.get("broken") is None


def test_json_cache_preserves_updates_from_multiple_processes(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    processes = [multiprocessing.Process(target=_cache_writer, args=(str(path), prefix)) for prefix in ("a", "b")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert set(json.loads(path.read_text())["entries"]) == {
        *(f"a-{index}" for index in range(50)),
        *(f"b-{index}" for index in range(50)),
    }


def test_race_returns_without_waiting_for_losing_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "race", "order": ["e2b", "arker"]},
                "create": {"strategy": "priority", "order": ["e2b", "arker"]},
            }
        )
    )

    never_returns = threading.Event()

    def slow_miss() -> None:
        never_returns.wait()

    started = time.monotonic()
    found = routed_get(
        "vm-a",
        "e2b",
        {"router_config_path": config_path},
        {"e2b": slow_miss, "arker": lambda: object()},
    )

    assert found is not None and found[0] == "arker"
    assert time.monotonic() - started < 0.25


def test_lookup_evicts_cache_entry_for_removed_provider(tmp_path: Path) -> None:
    cache_path = tmp_path / "routes.json"
    JsonRouteCache(cache_path).set("vm-a", "removed-provider")
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["e2b", "arker"]},
                "create": {"strategy": "priority", "order": ["e2b", "arker"]},
                "cache": {"path": str(cache_path)},
            }
        )
    )

    found = routed_get(
        "vm-a",
        "e2b",
        {"router_config_path": config_path},
        {"e2b": lambda: object(), "arker": lambda: None},
    )

    assert found is not None and found[0] == "e2b"
    assert JsonRouteCache(cache_path).get("vm-a") == "e2b"


def test_routing_rejects_provider_not_configured_by_shim(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["modal", "e2b"]},
                "create": {"strategy": "priority", "order": ["modal", "e2b"]},
            }
        )
    )
    with pytest.raises(ValueError, match=r'unknown provider "modal"'):
        routed_get("vm-a", "e2b", {"router_config_path": config_path}, {"e2b": lambda: None})


def test_race_continues_after_one_provider_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "race", "order": ["e2b", "arker"]},
                "create": {"strategy": "priority", "order": ["e2b", "arker"]},
            }
        )
    )

    def fails() -> None:
        raise RuntimeError("temporary lookup failure")

    found = routed_get(
        "vm-a",
        "e2b",
        {"router_config_path": config_path},
        {"e2b": fails, "arker": lambda: object()},
    )
    assert found is not None and found[0] == "arker"


def test_missing_arker_source_falls_back_to_native_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["arker", "e2b"]},
                "create": {"strategy": "priority", "order": ["arker", "e2b"]},
            }
        )
    )

    def missing_source() -> None:
        raise ValueError("Arker source is required")

    provider, result = routed_create(
        "e2b",
        {"router_config_path": config_path},
        {
            "arker": missing_source,
            "e2b": lambda: type("NativeSandbox", (), {"sandbox_id": "native-e2b"})(),
        },
    )

    assert provider == "e2b"
    assert result.sandbox_id == "native-e2b"


def test_ambiguous_create_outcome_does_not_fall_through(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["e2b", "arker"]},
                "create": {"strategy": "priority", "order": ["e2b", "arker"]},
            }
        )
    )
    duplicate_creates = 0

    def ambiguous() -> None:
        raise RuntimeError("network failure: operation outcome is unknown")

    def duplicate() -> object:
        nonlocal duplicate_creates
        duplicate_creates += 1
        return object()

    with pytest.raises(RuntimeError, match="outcome is unknown"):
        routed_create(
            "e2b",
            {"router_config_path": config_path},
            {"e2b": ambiguous, "arker": duplicate},
        )
    assert duplicate_creates == 0


def test_native_not_found_errors_become_route_misses() -> None:
    class MissingError(Exception):
        status_code = 404

    assert get_or_none(lambda: (_ for _ in ()).throw(MissingError("missing"))) is None


def test_native_lookup_errors_are_not_hidden() -> None:
    with pytest.raises(RuntimeError, match="provider unavailable"):
        get_or_none(lambda: (_ for _ in ()).throw(RuntimeError("provider unavailable")))


def test_unknown_lookup_error_text_cannot_trigger_fallback() -> None:
    error = RuntimeError("Sandbox not found in upstream response")
    with pytest.raises(RuntimeError) as caught:
        get_or_none(lambda: (_ for _ in ()).throw(error))
    assert caught.value is error


@pytest.mark.parametrize("native", ["e2b", "modal"])
@pytest.mark.parametrize("strategy", ["priority", "race"])
@pytest.mark.parametrize("stale_cache", [False, True])
def test_native_first_lookup_accepts_an_arker_identifier(tmp_path, native, strategy, stale_cache) -> None:
    identifier = "vmh-b471cad989f2eab9-01M548DGAXJ48SGE3SRTRR8HNV"
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": strategy, "order": [native, "arker"]},
                "create": {"strategy": "priority", "order": ["arker"]},
                "cache": {"path": str(tmp_path / "cache.json")},
            }
        )
    )

    def incompatible():
        raise ValueError("native SDK cannot represent an Arker VM identifier")

    if stale_cache:
        JsonRouteCache(tmp_path / "cache.json").set(identifier, native)

    assert routed_get(
        identifier,
        native,
        {"router_config_path": str(path)},
        {
            native: incompatible,
            "arker": lambda: "found",
        },
    ) == ("arker", "found")


def test_shared_identifier_conformance_and_native_only_eligibility() -> None:
    cases = json.loads((Path(__file__).resolve().parents[3] / "compatibility/router-identifiers.json").read_text())
    for case in cases:
        assert is_arker_vm_identifier(case["id"]) is case["arker"]
        for provider in ["e2b", "modal", "blaxel", "daytona", "arker"]:
            assert provider_accepts_identifier(provider, case["id"], arker_enabled=False)
            assert provider_accepts_identifier(provider, case["id"], arker_enabled=True) is (
                not case["arker"] or provider not in {"e2b", "modal"}
            )


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("modal.exception", "NotFoundError"),
        ("e2b.exceptions", "SandboxNotFoundException"),
        ("daytona.common.errors", "DaytonaNotFoundError"),
    ],
)
def test_typed_native_absence_does_not_require_message_guessing(monkeypatch, module_name, class_name) -> None:
    missing_type = type(class_name, (Exception,), {"__module__": module_name})
    monkeypatch.setattr("arker._compat.import_optional", lambda _: types.SimpleNamespace(**{class_name: missing_type}))
    assert get_or_none(lambda: (_ for _ in ()).throw(missing_type("opaque"))) is None


def test_cache_cannot_override_configured_providers(tmp_path: Path) -> None:
    cache_path = tmp_path / "ownership.json"
    JsonRouteCache(cache_path).set("existing", "arker")
    config_path = tmp_path / "computesdk_router.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["e2b"]},
                "create": {"strategy": "priority", "order": ["e2b"]},
                "cache": {"path": str(cache_path)},
            }
        )
    )

    def excluded() -> None:
        raise AssertionError("excluded provider queried")

    found = routed_get(
        "existing",
        "e2b",
        {"router_config_path": config_path},
        {
            "arker": excluded,
            "e2b": lambda: "native",
        },
    )
    assert found == ("e2b", "native")
