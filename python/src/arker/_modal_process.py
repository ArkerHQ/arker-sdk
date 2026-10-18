"""Back Modal's native process/stream objects with the shared guest protocol."""

from __future__ import annotations

import asyncio
import base64
import time


def modal_process(processes, identifier, *, text=True, by_line=False, stdout=None, stderr=None, deadline=None):
    from modal._utils.async_utils import synchronize_api
    from modal.container_process import _ContainerProcess
    from modal.exception import ExecTimeoutError
    from modal.stream_type import StreamType
    from modal_proto import task_command_router_pb2 as wire

    class Router:
        def check(self, limit):
            if limit is not None and time.monotonic() >= limit:
                raise ExecTimeoutError(f"Deadline exceeded for exec {identifier}")

        async def status(self, limit, response):
            self.check(limit)
            current = await asyncio.to_thread(processes.get, identifier, raw_exit_code=True)
            self.check(limit)
            if current.status == "running":
                return response()
            code = current.exit_code
            return response(signal=-code) if code < 0 else response(code=code)

        async def exec_poll(self, task_id, exec_id, deadline=None):
            return await self.status(deadline, wire.TaskExecPollResponse)

        async def exec_wait(self, task_id, exec_id, deadline=None):
            while True:
                response = await self.status(deadline, wire.TaskExecWaitResponse)
                if response.WhichOneof("exit_status") is not None:
                    return response
                await asyncio.sleep(0.05)

        async def exec_stdio_read(self, task_id, exec_id, file_descriptor, deadline=None, start_offset=0):
            offset = start_offset
            stream = "stdout" if file_descriptor == 1 else "stderr"
            while True:
                self.check(deadline)
                frame = await asyncio.to_thread(
                    processes._call, "readStream", identifier=identifier, stream=stream, offset=offset
                )
                self.check(deadline)
                data = base64.b64decode(frame["data"], validate=True)
                offset = frame["nextOffset"]
                if data:
                    yield wire.TaskExecStdioReadResponse(data=data)
                if frame["eof"]:
                    return
                if not data:
                    await asyncio.sleep(0.05)

        async def exec_stdin_write(self, *, task_id, exec_id, offset, data, eof):
            if data:
                await asyncio.to_thread(processes.write_stdin, identifier, data)
            if eof:
                await asyncio.to_thread(processes.close_stdin, identifier)

    async def create():
        return _ContainerProcess(
            process_id=identifier,
            task_id="arker",
            client=None,
            command_router_client=Router(),
            stdout=StreamType.PIPE if stdout is None else stdout,
            stderr=StreamType.PIPE if stderr is None else stderr,
            exec_deadline=deadline,
            text=text,
            by_line=by_line,
        )

    return synchronize_api(create)()
