import { ContainerProcess, type SandboxExecParams } from "modal";
import { setTimeout as delay } from "node:timers/promises";
import {
  GuestProcessError,
  type GuestProcesses,
  type GuestProcessResult,
} from "./guest-process.js";

type Processes = Pick<
  GuestProcesses,
  "get" | "readOutput" | "writeStdin" | "closeStdin" | "kill"
>;

export function modalCommandHandle(
  processes: Processes,
  current: GuestProcessResult,
  params: SandboxExecParams & { mode: "binary" },
): ContainerProcess<Uint8Array>;
export function modalCommandHandle(
  processes: Processes,
  current: GuestProcessResult,
  params?: SandboxExecParams & { mode?: "text" },
): ContainerProcess<string>;
export function modalCommandHandle(
  processes: Processes,
  current: GuestProcessResult,
  params: SandboxExecParams = {},
) {
  const id = current.pid;
  const deadline = params.timeoutMs ? Date.now() + params.timeoutMs : null;
  function check(
    limit: number | null,
    operation: string,
    signal?: AbortSignal,
  ) {
    signal?.throwIfAborted();
    if (limit !== null && Date.now() >= limit)
      throw new Error(`Deadline exceeded while ${operation} for exec ${id}`);
  }
  async function poll(limit: number | null, signal?: AbortSignal) {
    check(limit, "polling", signal);
    const result = await processes.get(id, { rawExitCode: true, signal });
    check(limit, "polling", signal);
    return result.status === "running"
      ? {}
      : result.exitCode < 0
        ? { signal: -result.exitCode }
        : { code: result.exitCode };
  }
  const client = {
    async execPoll(_task: string, _exec: string, limit: number | null) {
      return poll(limit);
    },
    async execWait(
      _task: string,
      _exec: string,
      limit: number | null,
      signal?: AbortSignal,
    ) {
      while (true) {
        check(limit, "waiting", signal);
        const value = await poll(limit, signal);
        if ("code" in value || "signal" in value) return value;
        await delay(50, undefined, { signal });
      }
    },
    async *execStdioRead(
      _task: string,
      _exec: string,
      fd: number,
      limit: number | null,
      signal?: AbortSignal,
    ) {
      const stream = fd === 1 ? "stdout" : "stderr";
      let offset = 0;
      try {
        while (true) {
          check(limit, "reading output", signal);
          const chunk = await processes.readOutput(id, stream, offset, signal);
          check(limit, "reading output", signal);
          offset = chunk.nextOffset;
          if (chunk.data.length) yield { data: chunk.data };
          if (chunk.eof) return;
          if (!chunk.data.length) await delay(50, undefined, { signal });
        }
      } catch (error) {
        if (!signal?.aborted) throw error;
      }
    },
    async execStdinWrite(
      _task: string,
      _exec: string,
      _offset: number,
      data: Uint8Array,
      eof: boolean,
    ) {
      if (data.length) await processes.writeStdin(id, data);
      if (eof) await processes.closeStdin(id);
    },
    async execStdinWriteStream(
      _task: string,
      _exec: string,
      source: { readFrom(offset: number): AsyncIterable<Uint8Array> },
    ) {
      let total = 0;
      try {
        for await (const chunk of source.readFrom(0)) {
          check(deadline, "writing stdin");
          if (chunk.length) await processes.writeStdin(id, chunk);
          total += chunk.length;
        }
        await processes.closeStdin(id);
      } catch (error) {
        if (
          error instanceof GuestProcessError &&
          error.code === "process_stdin_closed"
        ) {
          const status = await poll(deadline);
          if ("code" in status || "signal" in status) return total;
        }
        // EOF would commit a truncated filesystem upload. Keep its primary error.
        try {
          await processes.kill(id);
        } catch {}
        throw error;
      }
      return total;
    },
  };
  return new ContainerProcess<string | Uint8Array>(
    "arker",
    id,
    client as unknown as ConstructorParameters<typeof ContainerProcess>[2],
    params,
    deadline,
  );
}
