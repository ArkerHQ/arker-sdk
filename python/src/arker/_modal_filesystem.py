"""Reuse the native Modal filesystem frontend over the shared guest transport."""

from __future__ import annotations

import asyncio
import json
import weakref
from functools import lru_cache
from importlib.resources import files


class FilesystemContainer:
    def __init__(self, sandbox):
        self._sandbox = weakref.proxy(sandbox)
        self._source = files("arker").joinpath("_guest_filesystem.py").read_text()

    async def exec(self, *command, **options):
        from modal._utils.async_utils import synchronizer

        if len(command) != 2 or command[0] != "/__modal/.bin/modal-sandbox-fs-tools":
            raise ValueError("Unexpected Modal filesystem command")
        operation = json.loads(command[1])
        if "Watch" in operation:
            raise NotImplementedError("Modal filesystem watching needs an ordered guest event stream on Arker")
        process = await asyncio.to_thread(self._sandbox.exec, "python3", "-c", self._source, command[1], **options)
        return synchronizer._translate_in(process)


@lru_cache(maxsize=1)
def filesystem_type():
    from modal._utils.async_utils import synchronize_api
    from modal.sandbox._filesystem import _SandboxFilesystem

    class _ArkerFilesystem(_SandboxFilesystem):
        async def watch(self, *args, **kwargs):
            from ._compat import UnsupportedCompatibilityOption

            raise UnsupportedCompatibilityOption(
                "Modal filesystem watching needs an ordered guest event stream on Arker"
            )
            yield  # Keep the native async-iterator interface for an unsupported operation.

    return synchronize_api(_ArkerFilesystem)


def filesystem(container):
    return filesystem_type()(container)
