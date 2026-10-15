from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest

from arker._compat import routed_create, routed_get
from arker._router import JsonRouteCache, RouterConfig, load_router_config, select_weighted_provider


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

    def slow_miss() -> None:
        time.sleep(0.5)

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
