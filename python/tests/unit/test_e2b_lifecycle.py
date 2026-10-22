import asyncio
import types

import pytest

import arker.e2b as shim


@pytest.mark.parametrize("timeout", [None, 17])
def test_arker_creation_maps_e2b_lifetime_to_server_delete_schedule(timeout, monkeypatch):
    requests = []
    child = types.SimpleNamespace(id="child")
    config = {
        "source": "template",
        "policies": {
            "secrets": {"TOKEN": "synthetic"},
            "rules": {"custom": {"type": "outbound", "priority": 7, "action": "allow"}},
        },
    }
    monkeypatch.setattr(
        "arker._compat.arker_client",
        lambda config: types.SimpleNamespace(fork=lambda **request: requests.append(request) or child),
    )

    assert (
        shim.Sandbox._create_arker(
            config,
            "template",
            timeout,
            False,
            {},
        )
        is child
    )

    rules = requests[0]["policies"]["rules"]
    schedules = [rule for rule in rules.values() if rule["type"] == "schedule"]
    assert schedules == [
        {
            "type": "schedule",
            "after_created": 300 if timeout is None else timeout,
            "action": "delete",
        }
    ]
    assert len([rule for rule in rules.values() if rule["type"] == "outbound_guard"]) == 1
    assert rules["custom"] == {"type": "outbound", "priority": 7, "action": "allow"}
    assert requests[0]["policies"]["secrets"] == {"TOKEN": "synthetic"}
    assert set(config["policies"]["rules"]) == {"custom"}


@pytest.mark.parametrize("timeout", [True, 0, -1, 86_401, 1.5, "300"])
def test_arker_creation_rejects_invalid_e2b_lifetime_before_fork(timeout, monkeypatch):
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: pytest.fail("must not fork"))
    with pytest.raises((TypeError, ValueError), match="timeout"):
        shim.Sandbox._create_arker({}, None, timeout, None, {})


def test_arker_creation_rejects_reserved_lifetime_policy_without_fork(monkeypatch):
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: pytest.fail("must not fork"))
    with pytest.raises(ValueError, match="reserved"):
        shim.Sandbox._create_arker({"policies": {"rules": {"computesdk-e2b-lifetime": None}}}, None, None, None, {})


def test_arker_identity_validation_fails_closed_without_mutation(monkeypatch):
    vm = types.SimpleNamespace(id="custom", deleted=False, delete=lambda: pytest.fail("validation must not mutate"))
    monkeypatch.setattr(
        shim,
        "run_command",
        lambda vm, command: shim._CommonCommandResult(stdout="", stderr="mismatch", exit_code=1),
    )
    with pytest.raises(shim.UnsupportedCompatibilityOption, match="UID/GID 1000"):
        shim._validate_e2b_identity(vm)


def test_arker_creation_validation_deletes_only_the_known_child(monkeypatch):
    deleted = []
    child = types.SimpleNamespace(id="child", delete=lambda: deleted.append("child"))
    monkeypatch.setattr(shim, "routed_create", lambda *args, **kwargs: ("arker", child))
    monkeypatch.setattr(
        shim,
        "_validate_e2b_identity",
        lambda vm: (_ for _ in ()).throw(shim.UnsupportedCompatibilityOption("identity mismatch")),
    )
    with pytest.raises(shim.UnsupportedCompatibilityOption, match="identity mismatch"):
        shim.Sandbox.create()
    assert deleted == ["child"]


def test_native_filesystem_is_preserved_with_binary_options_and_metadata():
    marker = object()

    class NativeFiles:
        def read(self, path, **options):
            assert path == "/binary"
            assert options == {"format": "bytes", "user": "user", "request_timeout": 7}
            return bytearray(b"\x00\xff\x80")

        def write(self, path, content, **options):
            assert (path, content, options) == ("/binary", b"\xff", {"metadata": {"fixture": "owned"}})
            return marker

    files = NativeFiles()
    sandbox = shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", files=files))
    assert sandbox.files is files
    assert sandbox.files.read("/binary", format="bytes", user="user", request_timeout=7) == bytearray(b"\x00\xff\x80")
    assert sandbox.files.write("/binary", b"\xff", metadata={"fixture": "owned"}) is marker


def test_async_native_filesystem_is_preserved_without_sync_translation():
    from arker._compat_e2b_async import AsyncSandbox

    class NativeFiles:
        async def read(self, path, **options):
            assert (path, options) == ("/binary", {"format": "bytes", "request_timeout": 7})
            return bytearray(b"\xff")

    files = NativeFiles()
    sandbox = AsyncSandbox(shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", files=files)))
    assert sandbox.files is files
    assert asyncio.run(sandbox.files.read("/binary", format="bytes", request_timeout=7)) == bytearray(b"\xff")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_arker_binary_reads_preserve_every_byte_and_native_return_type(asynchronous, monkeypatch):
    payload = bytes(range(256))
    vm = types.SimpleNamespace(id="arker", sync=lambda path: payload)
    monkeypatch.setattr(
        shim,
        "run_command",
        lambda vm, command, **kwargs: shim._CommonCommandResult(stdout="", stderr="", exit_code=0),
    )
    sandbox = shim.Sandbox(vm=vm)
    if asynchronous:
        from arker._compat_e2b_async import AsyncSandbox

        sandbox = AsyncSandbox(sandbox)
        result = asyncio.run(sandbox.files.read("/binary", format="bytes"))
    else:
        result = sandbox.files.read("/binary", format="bytes")
    assert type(result) is bytearray
    assert result == bytearray(payload)


def test_arker_stream_reads_are_not_faked_with_a_buffer():
    from arker._compat import UnsupportedCompatibilityOption

    def unexpected_read(path):
        pytest.fail("unsupported formats must not perform filesystem I/O")

    sandbox = shim.Sandbox(vm=types.SimpleNamespace(id="arker", sync=unexpected_read))
    with pytest.raises(UnsupportedCompatibilityOption, match="format"):
        sandbox.files.read("/binary", format="stream")


def test_arker_writes_and_metadata_match_e2b_default_user(monkeypatch):
    commands = []
    staged = []
    vm = types.SimpleNamespace(id="arker", sync=lambda path, content: staged.append((path, content)))
    monkeypatch.setattr(
        shim,
        "run_command",
        lambda vm, command, **kwargs: (
            commands.append(command)
            or shim._CommonCommandResult(
                stdout="/tmp/file\x1f4\x1f81a4\x1f-rw-r--r--\x1fuser\x1fuser\x1f1\x1fregular file",
                stderr="",
                exit_code=0,
            )
        ),
    )
    files = shim.Sandbox(vm=vm).files
    files.write("/tmp/file", "data")
    info = files.get_info("/tmp/file")
    assert commands[0].startswith("runuser -u user -- sh -c ")
    assert "cat -- /tmp/.computesdk-e2b-write-" in commands[0]
    assert "> /tmp/file" in commands[0]
    assert commands[1].startswith("rm -f -- /tmp/.computesdk-e2b-write-")
    assert staged[0][0].startswith("/tmp/.computesdk-e2b-write-")
    assert info.permissions == "-rw-r--r--"
    assert (info.owner, info.group) == ("user", "user")


def test_arker_write_preserves_user_permission_failure_and_cleans_staging(monkeypatch):
    commands = []
    vm = types.SimpleNamespace(id="arker", sync=lambda path, content: None)

    def run(vm, command, **kwargs):
        commands.append(command)
        denied = command.startswith("runuser ")
        return shim._CommonCommandResult(
            stdout="", stderr="Permission denied" if denied else "", exit_code=1 if denied else 0
        )

    monkeypatch.setattr(shim, "run_command", run)
    with pytest.raises(RuntimeError, match="Permission denied"):
        shim.Sandbox(vm=vm).files.write("/root/owned", "data")
    assert commands[0].startswith("runuser -u user -- ")
    assert commands[1].startswith("rm -f -- /tmp/.computesdk-e2b-write-")


@pytest.mark.parametrize("alias", ["timeout_ms", "timeoutMs"])
def test_python_create_does_not_misread_millisecond_alias_as_seconds(alias, monkeypatch):
    monkeypatch.setattr(shim, "routed_create", lambda *args, **kwargs: pytest.fail("must not route"))
    with pytest.raises(TypeError, match="E2B Python API uses timeout in seconds"):
        shim.Sandbox.create(**{alias: 300_000})


def test_arker_creation_preserves_legacy_ordered_policy(monkeypatch):
    requests = []
    ordered = [{"type": "outbound", "priority": 1, "action": "deny"}]
    config = {"source": "template", "policies": {"policies": ordered}}
    monkeypatch.setattr(
        "arker._compat.arker_client",
        lambda config: types.SimpleNamespace(fork=lambda **request: requests.append(request) or object()),
    )

    shim.Sandbox._create_arker(config, None, None, None, {})

    assert requests[0]["policies"]["policies"] == ordered
    assert list(requests[0]["policies"]["rules"].values()) == [
        {"type": "schedule", "after_created": 300, "action": "delete"}
    ]
    assert config == {"source": "template", "policies": {"policies": ordered}}


def test_arker_write_cleans_partial_staging_after_sync_failure(monkeypatch):
    commands = []

    def fail_sync(path, content):
        raise OSError("upload interrupted")

    monkeypatch.setattr(
        shim,
        "run_command",
        lambda vm, command, **kwargs: (
            commands.append(command) or shim._CommonCommandResult(stdout="", stderr="", exit_code=0)
        ),
    )
    with pytest.raises(OSError, match="upload interrupted"):
        shim.Sandbox(vm=types.SimpleNamespace(id="arker", sync=fail_sync)).files.write("/tmp/file", "data")
    assert len(commands) == 1
    assert commands[0].startswith("rm -f -- /tmp/.computesdk-e2b-write-")


@pytest.mark.parametrize("deleted", [True, False])
def test_kill_preserves_provider_boolean(deleted):
    assert (
        shim.Sandbox(
            vm=types.SimpleNamespace(
                id="vm", refresh=lambda: None, delete=lambda: types.SimpleNamespace(deleted=deleted)
            )
        ).kill()
        is deleted
    )
    assert shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", kill=lambda: deleted)).kill() is deleted


def test_context_manager_cleans_up_without_swallowing_user_error():
    calls = []
    sandbox = shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", kill=lambda: calls.append("kill")))
    failure = RuntimeError("application failed")
    with pytest.raises(RuntimeError) as raised, sandbox as entered:
        assert entered is sandbox
        raise failure
    assert raised.value is failure
    assert calls == ["kill"]


@pytest.mark.parametrize("status", [404, 403, 503])
def test_kill_only_treats_authoritative_absence_as_false(status):
    from arker.computer import ArkerError

    error = ArkerError("request_failed", "request failed", status=status)

    def delete():
        raise error

    sandbox = shim.Sandbox(vm=types.SimpleNamespace(id="vm", refresh=lambda: None, delete=delete))
    if status == 404:
        assert sandbox.kill() is False
    else:
        with pytest.raises(ArkerError) as raised:
            sandbox.kill()
        assert raised.value is error


def test_connect_probes_metadata_before_connecting_only_the_winner(monkeypatch):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append(("info", identifier)) or object(),
        connect=lambda identifier: (
            calls.append(("connect", identifier)) or types.SimpleNamespace(sandbox_id=identifier)
        ),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))
    monkeypatch.setattr(shim, "_validate_e2b_identity", lambda vm: None)

    def route(identifier, provider, config, getters):
        getters["e2b"]()
        assert calls == [("info", identifier)]
        return "arker", types.SimpleNamespace(id="arker-winner")

    monkeypatch.setattr(shim, "routed_get", route)
    assert shim.Sandbox.connect("shared-id").sandbox_id == "arker-winner"
    assert calls == [("info", "shared-id")]


def test_connect_resolves_native_handle_after_native_ownership_wins(monkeypatch):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append("info") or object(),
        connect=lambda identifier: calls.append("connect") or types.SimpleNamespace(sandbox_id=identifier),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))
    monkeypatch.setattr(shim, "routed_get", lambda identifier, provider, config, getters: ("e2b", getters["e2b"]()))
    assert shim.Sandbox.connect("native-id").sandbox_id == "native-id"
    assert calls == ["info", "connect"]


@pytest.mark.parametrize("owner", ["e2b", "arker", None])
def test_static_kill_uses_shared_ownership_without_connecting(monkeypatch, owner):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append("info") or object(),
        connect=lambda identifier: pytest.fail("Deletion must not connect"),
        kill=lambda identifier: calls.append(("native-kill", identifier)) or True,
    )
    vm = types.SimpleNamespace(
        id="shared-id",
        refresh=lambda: None,
        delete=lambda: calls.append("arker-kill") or types.SimpleNamespace(deleted=True),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))

    def lookup(identifier, provider, config, getters):
        assert identifier == "shared-id"
        if owner == "e2b":
            return owner, getters[owner]()
        return (owner, vm) if owner else None

    monkeypatch.setattr(shim, "routed_get", lookup)
    assert shim.Sandbox.kill(sandbox_id="shared-id") is (owner is not None)
    assert calls == (["info", ("native-kill", "shared-id")] if owner == "e2b" else ["arker-kill"] if owner else [])
