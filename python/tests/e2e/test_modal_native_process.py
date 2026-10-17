from __future__ import annotations

import os

import pytest

from arker.modal import Sandbox


@pytest.mark.skipif(
    not all(os.environ.get(key) for key in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")),
    reason="native Modal credentials are required",
)
def test_native_binary_process_and_termination() -> None:
    modal = pytest.importorskip("modal")
    native = modal.Sandbox.create(
        app=modal.App.lookup("arker-compat-image-test", create_if_missing=True),
        timeout=120,
    )
    sandbox = Sandbox(native=native)
    try:
        process = sandbox.exec("sh", "-c", "cat; printf error >&2", text=False)
        process.stdin.write(b"parity\x00test\n")
        process.stdin.write_eof()
        process.stdin.drain()
        assert process.stdout.read() == b"parity\x00test\n"
        assert process.stderr.read() == b"error"
        assert process.wait() == 0
    finally:
        assert isinstance(sandbox.terminate(wait=True), int)
