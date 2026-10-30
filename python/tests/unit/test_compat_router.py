from __future__ import annotations

import asyncio
import enum
import json
import multiprocessing
import threading
import time
import types
from pathlib import Path

import pytest

from arker._compat import arker_source, get_or_none, routed_create, routed_get, router_config
from arker._router import (
    JsonRouteCache,
    RouterConfig,
    RouterLookupTimeoutError,
    is_arker_vm_identifier,
    load_router_config,
    provider_accepts_identifier,
    select_weighted_provider,
)


def _cache_writer(path: str, prefix: str) -> None:
    cache = JsonRouteCache(path, max_entries=1_000, max_bytes=1_000_000)
    for index in range(50):
        cache.set(f"{prefix}-{index}", "arker")


def _write_router(path, *, order=("e2b", "arker"), strategy="priority", create=None, **fields):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": strategy, "order": list(order)},
                "create": create if create is not None else {"strategy": "priority", "order": list(order)},
                **fields,
            }
        )
    )


@pytest.fixture
def router_file(tmp_path: Path) -> Path:
    path = tmp_path / "computesdk_router.json"
    _write_router(
        path,
        strategy="race",
        create={"strategy": "weighted", "weights": {"e2b": 90, "arker": 10}, "fallback": ["e2b", "arker"]},
        cache={"path": str(tmp_path / "routes.json"), "maxBytes": 4096, "maxEntries": 10},
    )
    return path


def test_loads_same_versioned_schema_as_typescript(router_file: Path) -> None:
    config = load_router_config(router_file)
    assert config == RouterConfig.from_dict(json.loads(router_file.read_text()))
    assert config.existing.order == ("e2b", "arker")
    assert config.create.weights == {"e2b": 90, "arker": 10}


_CONFIG_CASES = json.loads((Path(__file__).resolve().parents[3] / "compatibility/router-config-cases.json").read_text())


@pytest.mark.parametrize("case", _CONFIG_CASES["cases"], ids=lambda case: case["name"])
def test_shared_router_config_contract(case):
    raw = {**_CONFIG_CASES["base"], **case["patch"]}
    if "error" in case:
        with pytest.raises(ValueError, match=case["error"]):
            RouterConfig.from_dict(raw)
    else:
        assert RouterConfig.from_dict(raw).version == 1


def test_large_weights_do_not_overflow() -> None:
    weights = {"blaxel": 1e308, "arker": 1e308}
    assert select_weighted_provider(weights, random_value=0.25) == "blaxel"
    assert select_weighted_provider(weights, random_value=0.75) == "arker"


@pytest.mark.parametrize("status", [403, 429, 500])
@pytest.mark.parametrize("error_first", [True, False])
def test_race_never_masks_provider_errors(router_file, status, error_first):
    error = RuntimeError(f"lookup HTTP {status}")
    error.status_code = status
    first_finished = threading.Event()

    def fail():
        if not error_first:
            assert first_finished.wait(2)
        first_finished.set()
        raise error

    def found():
        if error_first:
            assert first_finished.wait(2)
        first_finished.set()
        return object()

    with pytest.raises(RuntimeError) as caught:
        routed_get("shared", "e2b", {"router_config_path": str(router_file)}, {"e2b": fail, "arker": found})
    assert caught.value is error


def test_race_two_hits_respect_configured_priority(router_file):
    lower_finished = threading.Event()
    higher = object()

    def higher_priority():
        assert lower_finished.wait(2)
        return higher

    def lower_priority():
        lower_finished.set()
        return object()

    assert routed_get(
        "shared",
        "e2b",
        {"router_config_path": str(router_file)},
        {
            "e2b": higher_priority,
            "arker": lower_priority,
        },
    ) == ("e2b", higher)


def test_race_preserves_provider_cancellation(router_file):
    error = asyncio.CancelledError()

    def cancelled():
        raise error

    with pytest.raises(asyncio.CancelledError) as caught:
        routed_get(
            "shared",
            "e2b",
            {"router_config_path": str(router_file)},
            {
                "e2b": cancelled,
                "arker": lambda: object(),
            },
        )
    assert caught.value is error


def test_race_errors_follow_priority_not_completion(router_file):
    lower_finished = threading.Event()
    preferred_error = PermissionError("preferred provider denied access")

    def preferred():
        assert lower_finished.wait(2)
        raise preferred_error

    def lower():
        lower_finished.set()
        raise RuntimeError("lower provider unavailable")

    with pytest.raises(PermissionError) as caught:
        routed_get("shared", "e2b", {"router_config_path": str(router_file)}, {"e2b": preferred, "arker": lower})
    assert caught.value is preferred_error


@pytest.mark.parametrize("strategy", ["priority", "race"])
def test_lookup_deadline_does_not_cache_late_success(router_file, strategy):
    raw = json.loads(router_file.read_text())
    raw["existing"].update(strategy=strategy, timeoutMs=20)
    router_file.write_text(json.dumps(raw))
    release = threading.Event()
    completed = threading.Event()

    def late_hit():
        assert release.wait(2)
        completed.set()
        return object()

    try:
        with pytest.raises(RouterLookupTimeoutError):
            routed_get(
                "shared",
                "e2b",
                {"router_config_path": str(router_file)},
                {
                    "e2b": late_hit,
                    "arker": lambda: None,
                },
            )
    finally:
        release.set()
    assert completed.wait(2)
    assert JsonRouteCache(raw["cache"]["path"]).get("shared") is None


def test_cached_probe_and_fallback_share_one_lookup_budget(router_file, monkeypatch):
    raw = json.loads(router_file.read_text())
    raw["existing"].update(strategy="priority", timeoutMs=30)
    router_file.write_text(json.dumps(raw))
    JsonRouteCache(raw["cache"]["path"]).set("shared", "e2b")
    now = [0.0]
    monkeypatch.setattr("arker._compat.time.monotonic", lambda: now[0])
    fallback_calls = []

    def cached_miss():
        now[0] = 0.031

    with pytest.raises(RouterLookupTimeoutError):
        routed_get(
            "shared",
            "e2b",
            {"router_config_path": str(router_file)},
            {
                "e2b": cached_miss,
                "arker": lambda: fallback_calls.append(True),
            },
        )
    assert fallback_calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, 0.5, float("inf"), 9_007_199_254_740_992])
def test_lookup_budget_rejects_invalid_config_before_io(router_file, timeout):
    raw = json.loads(router_file.read_text())
    raw["existing"]["timeoutMs"] = timeout
    with pytest.raises(ValueError, match="positive safe integer"):
        RouterConfig.from_dict(raw)


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
    _write_router(path, order=["blaxel", "arker"], cache={"path": str(cache_path)})
    routed_create("blaxel", {"router_config_path": str(path)}, {"blaxel": lambda: native, "arker": lambda: None})
    assert JsonRouteCache(cache_path).get(identifier) == "blaxel"


def test_successful_creation_never_falls_through_on_metadata_failure(tmp_path):
    class Created:
        @property
        def id(self):
            raise RuntimeError("metadata unavailable")

    path = tmp_path / "router.json"
    _write_router(path, order=["blaxel", "arker"])
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
    _write_router(path, order=["e2b"], strategy="race", apiKey="must-not-be-accepted")
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


@pytest.mark.parametrize(
    "payload", [None, [], True, 1, "cache", {"version": True, "entries": {"old": {"provider": "e2b", "accessedAt": 1}}}]
)
def test_json_cache_recovers_from_invalid_root(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(payload))
    cache = JsonRouteCache(path, now=lambda: 100)

    assert cache.get("old") is None
    cache.set("new", "arker")
    assert cache.get("new") == "arker"
    assert json.loads(path.read_text()) == {
        "version": 1,
        "entries": {"new": {"provider": "arker", "accessedAt": 101}},
    }


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


def test_race_waits_for_parallel_miss_before_accepting_hit(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    _write_router(config_path, strategy="race")

    hit_finished = threading.Event()

    def slow_miss() -> None:
        assert hit_finished.wait(2)

    def hit():
        hit_finished.set()
        return object()

    started = time.monotonic()
    found = routed_get(
        "vm-a",
        "e2b",
        {"router_config_path": config_path},
        {"e2b": slow_miss, "arker": hit},
    )

    assert found is not None and found[0] == "arker"
    assert time.monotonic() - started < 0.25


def test_lookup_evicts_cache_entry_for_removed_provider(tmp_path: Path) -> None:
    cache_path = tmp_path / "routes.json"
    JsonRouteCache(cache_path).set("vm-a", "removed-provider")
    config_path = tmp_path / "computesdk_router.json"
    _write_router(config_path, cache={"path": str(cache_path)})

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
    _write_router(config_path, order=["modal", "e2b"])
    with pytest.raises(ValueError, match=r'unknown provider "modal"'):
        routed_get("vm-a", "e2b", {"router_config_path": config_path}, {"e2b": lambda: None})


def test_race_propagates_one_provider_error(tmp_path: Path) -> None:
    config_path = tmp_path / "computesdk_router.json"
    _write_router(config_path, strategy="race")

    def fails() -> None:
        raise RuntimeError("temporary lookup failure")

    with pytest.raises(RuntimeError, match="temporary lookup failure"):
        routed_get(
            "vm-a",
            "e2b",
            {"router_config_path": config_path},
            {"e2b": fails, "arker": lambda: object()},
        )


def test_missing_arker_source_falls_back_to_native_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ARKER_SOURCE", raising=False)
    monkeypatch.delenv("ARKER_SOURCE_VM", raising=False)
    config_path = tmp_path / "computesdk_router.json"
    _write_router(config_path, order=["arker", "e2b"])

    def missing_source() -> None:
        arker_source({})

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


def _wrapped_create_failure(failure):
    for _ in range(8):
        wrapper = RuntimeError("provider creation failed")
        wrapper.__cause__ = failure
        failure = wrapper
    return failure


def _cyclic_create_failure():
    failure = RuntimeError("cyclic provider error")
    failure.__cause__ = failure
    return failure


def _grpc_create_failure():
    status = enum.Enum("Status", {"UNAVAILABLE": 14}, module="grpclib.const")
    return type("RPCFailure", (RuntimeError,), {"status": status.UNAVAILABLE})("opaque")


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("opaque provider failure"),
        ValueError("opaque provider failure"),
        _wrapped_create_failure(RuntimeError("opaque provider failure")),
        type("RateLimited", (RuntimeError,), {"status": 429})("opaque"),
        RuntimeError("network failure: operation outcome is unknown"),
        ConnectionResetError("connection reset"),
        BrokenPipeError("broken pipe"),
        TypeError("fetch failed"),
        _wrapped_create_failure(RuntimeError("unauthorized")),
        _wrapped_create_failure(OSError("read failed")),
        _cyclic_create_failure(),
        type("Conflict", (RuntimeError,), {"status": 409})("exists"),
        type("ServerFailure", (RuntimeError,), {"status": 503})("opaque"),
        type("Conflict", (RuntimeError,), {"code": "SANDBOX_ALREADY_EXISTS"})("exists"),
        type("AlreadyExistsError", (RuntimeError,), {})("opaque"),
        type("InvalidError", (RuntimeError,), {})("opaque"),
        _grpc_create_failure(),
    ],
)
def test_ambiguous_create_outcome_does_not_fall_through(tmp_path: Path, failure: Exception) -> None:
    config_path = tmp_path / "computesdk_router.json"
    _write_router(config_path)
    duplicate_creates = 0

    def ambiguous() -> None:
        raise failure

    def duplicate() -> object:
        nonlocal duplicate_creates
        duplicate_creates += 1
        return object()

    with pytest.raises(type(failure)) as raised:
        routed_create(
            "e2b",
            {"router_config_path": config_path},
            {"e2b": ambiguous, "arker": duplicate},
        )
    assert raised.value is failure
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
    _write_router(
        path,
        order=[native, "arker"],
        strategy=strategy,
        create={"strategy": "priority", "order": ["arker"]},
        cache={"path": str(tmp_path / "cache.json")},
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
    _write_router(config_path, order=["e2b"], cache={"path": str(cache_path)})

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


@pytest.mark.parametrize("identifier", ["__proto__", "constructor", "toString", "é" * 10])
def test_cache_keys_and_utf8_byte_bound(tmp_path, identifier):
    path = tmp_path / "cache.json"
    cache = JsonRouteCache(path, max_bytes=100, now=lambda: 0)
    assert cache.get(identifier) is None
    cache.set(identifier, "arker")
    assert cache.get(identifier) == "arker"
    assert path.stat().st_size <= 100
    cache.delete(identifier)
    assert cache.get(identifier) is None


def test_cache_bound_smaller_than_empty_payload(tmp_path):
    path = tmp_path / "cache.json"
    cache = JsonRouteCache(path, max_bytes=1)
    cache.set("id", "arker")
    assert not path.exists() or path.stat().st_size <= 1
    assert cache.get("id") is None


def test_toml_router_config_is_rejected(tmp_path):
    path = tmp_path / "router.toml"
    path.write_text('version = 1\n[existing]\nstrategy = "priority"\norder = ["arker"]\n')
    with pytest.raises(ValueError):
        load_router_config(path)


@pytest.mark.parametrize("strategy", ["priority", "race"])
def test_cached_miss_invalidates_but_error_preserves_hint(router_file, strategy):
    raw = json.loads(router_file.read_text())
    raw["existing"]["strategy"] = strategy
    router_file.write_text(json.dumps(raw))
    cache = JsonRouteCache(raw["cache"]["path"])
    cache.set("shared", "e2b")
    error = RuntimeError("lookup unavailable")

    def fail():
        raise error

    def unexpected():
        pytest.fail("cached lookup failure must not fall through")

    config = {"router_config_path": router_file}
    with pytest.raises(RuntimeError) as caught:
        routed_get("shared", "e2b", config, {"e2b": fail, "arker": unexpected})
    assert caught.value is error
    assert cache.get("shared") == "e2b"
    assert routed_get("shared", "e2b", config, {"e2b": lambda: None, "arker": lambda: None}) is None
    assert cache.get("shared") is None


@pytest.mark.parametrize("reverse", [False, True])
def test_keyed_weighted_creation_is_stable_without_fallback(tmp_path, monkeypatch, reverse):
    from arker._compat import CompatibilityCreationNotStartedError

    path = tmp_path / "keyed.json"
    weights = {"arker": 50, "e2b": 50}
    if reverse:
        weights = dict(reversed(list(weights.items())))
    _write_router(path, create={"strategy": "weighted", "weights": weights, "fallback": ["e2b", "arker"]})
    config = {"router_config_path": str(path)}
    creators = {name: lambda name=name: types.SimpleNamespace(id=name) for name in weights}
    observed = [routed_create("e2b", config, creators, creation_key="repeat-key")[0] for _ in range(20)]
    assert set(observed) == {"arker"}  # Shared cross-language SHA-256 routing vector.
    calls = []

    def fail():
        calls.append("arker")
        raise CompatibilityCreationNotStartedError("provider unavailable")

    creators["arker"] = fail
    creators["e2b"] = lambda: pytest.fail("keyed creation must never change owner")
    with pytest.raises(CompatibilityCreationNotStartedError):
        routed_create("e2b", config, creators, creation_key="repeat-key")
    assert calls == ["arker"]
