import { RequestIdleTimeoutError } from "../internal/idle-fetch.js";
import { withRequestTransport } from "../internal/request-context.js";
import { createHash, randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { DaytonaConnectionTimeoutError, DaytonaError, DaytonaInternalServerError, DaytonaNotFoundError, DaytonaSessionEndedError } from "@daytona/sdk";
import type { SandboxInterface } from "computesdk";
import { shellQuote } from "./common.js";
import { GuestProcesses } from "./guest-process.js";
import source from "./daytona-session.generated.js";
import accountSource from "./daytona-pty.generated.js";

interface Command { id: string; command: string; exitCode?: number }
interface Session { sessionId: string; commands: Command[] }
interface Logs { output: string; stdout: string; stderr: string }
interface Result extends Partial<Logs> { cmdId: string; exitCode?: number }
type Handler = (text: string) => void | Promise<void>;
export interface SessionAPI {
  createSession(id: string): Promise<void>;
  getSession(id: string): Promise<Session>;
  listSessions(): Promise<Session[]>;
  deleteSession(id: string): Promise<void>;
  getSessionCommand(id: string, commandId: string): Promise<Command>;
  executeSessionCommand(id: string, request: { command: string; runAsync?: boolean; suppressInputEcho?: boolean }, timeout?: number): Promise<Result>;
  getSessionCommandLogs(id: string, commandId: string): Promise<Logs>;
  getSessionCommandLogs(id: string, commandId: string, onStdout: Handler, onStderr: Handler): Promise<void>;
  sendSessionCommandInput(id: string, commandId: string, data: string): Promise<void>;
}
interface WireCommand { id: string; command: string; exit_code: number | null }
interface WireSession { session_id: string; commands: WireCommand[] }
const command = (value: WireCommand): Command => ({ id: value.id, command: value.command, exitCode: value.exit_code ?? undefined });
const session = (value: WireSession): Session => ({ sessionId: value.session_id, commands: value.commands.map(command) });
const prefix = "daytona-session:";

export function createDaytonaSessions(box: SandboxInterface): SessionAPI {
  const processes = new GuestProcesses(box);
  const path = `/usr/local/lib/arker-compat/daytona-session-${createHash("sha256").update(source).digest("hex").slice(0, 16)}.py`;
  let installed: Promise<void> | undefined;
  async function install() {
    installed ??= box.filesystem.writeFile(path, source).then(() => undefined).catch(error => { installed = undefined; throw error; });
    await installed;
  }
  async function call<T>(action: string, values: Record<string, unknown> = {}): Promise<T> {
    await install();
    const payload = Buffer.from(JSON.stringify({ action, ...values })).toString("base64");
    const result = await box.runCommand(`python3 -I ${shellQuote(path)} call ${shellQuote(payload)}`);
    const response = JSON.parse(result.stdout) as { result: T; error?: string; code?: string };
    if (response.error && response.code === "input_closed") throw new DaytonaInternalServerError(response.error, 500);
    if (response.error && response.code === "session_ended") throw new DaytonaSessionEndedError(response.error, 410);
    if (response.error) throw response.code === "not_found" ? new DaytonaNotFoundError(response.error) : new DaytonaError(response.error);
    if (result.exitCode !== 0) throw new DaytonaError("Session control failed");
    return response.result;
  }
  async function read(id: string, commandId: string, stream: string, offset: number) {
    return call<{ data: string; eof: boolean }>("read", { session_id: id, command_id: commandId, stream, offset });
  }
  async function logs(id: string, commandId: string): Promise<Logs> {
    const values = { stdout: "", stderr: "", output: "" };
    for (const stream of ["stdout", "stderr", "output"] as const) {
      let offset = 0;
      const chunks: Buffer[] = [];
      for (;;) {
        const chunk = Buffer.from((await read(id, commandId, stream, offset)).data, "base64");
        offset += chunk.length;
        chunks.push(chunk);
        if (chunk.length < 32768) break;
      }
      values[stream] = Buffer.concat(chunks).toString("utf8");
    }
    return values;
  }
  async function follow(id: string, commandId: string, stdout: Handler, stderr: Handler) {
    const offsets = { stdout: 0, stderr: 0 };
    const decoders = { stdout: new TextDecoder(), stderr: new TextDecoder() };
    const finished = new Set<string>();
    while (finished.size < 2) {
      for (const stream of ["stdout", "stderr"] as const) {
        if (finished.has(stream)) continue;
        const result = await read(id, commandId, stream, offsets[stream]);
        const bytes = Buffer.from(result.data, "base64");
        offsets[stream] += bytes.length;
        const text = decoders[stream].decode(bytes, { stream: !result.eof });
        if (text) await (stream === "stdout" ? stdout : stderr)(text);
        if (result.eof) finished.add(stream);
      }
      if (finished.size < 2) await delay(20);
    }
  }
  const api = {
    async createSession(id: string) {
      if (typeof id !== "string" || !id) throw new TypeError("Session ID must be a nonempty string");
      await install();
      const account = await box.runCommand(`python3 -I -c ${shellQuote(accountSource)}`);
      if (account.exitCode !== 0) throw new DaytonaError("Unable to resolve session account");
      const [home] = JSON.parse(account.stdout) as [string];
      const name = prefix + randomUUID();
      let started;
      try { started = await processes.exec({ name, exclusiveName: prefix + id,
        command: JSON.stringify({ id }), argv: ["python3", "-I", path, "serve", id], user: "daytona", workingDir: home,
        preserveDescendants: true, env: { HOME: home, USER: "daytona", LOGNAME: "daytona" } }); }
      catch (error) {
        if (error instanceof RequestIdleTimeoutError) Object.defineProperties(error, {sessionId: {value: id}, processId: {value: name}});
        throw error;
      }
      try {
        const deadline = Date.now() + 30_000;
        for (;;) {
          try { await call("get", { session_id: id }); return; }
          catch (error) {
            if (error instanceof RequestIdleTimeoutError) throw error;
            if ((await processes.get(started.name)).status !== "running" || Date.now() >= deadline) throw error;
            await delay(20);
          }
        }
      } catch (error) {
        if (error instanceof Error) {
          try { Object.defineProperties(error, { sessionId: { value: id }, processId: { value: started.name } }); } catch {}
        }
        if (error instanceof RequestIdleTimeoutError) throw error;
        try { await withRequestTransport(undefined, () => processes.kill(started.name)); }
        catch (cleanup) {
          if (error instanceof Error) {
            try { Object.defineProperty(error, "cleanupError", { value: cleanup }); } catch {}
          }
        }
        throw error;
      }
    },
    async getSession(id: string) { return session(await call<WireSession>("get", { session_id: id })); },
    async listSessions() { return (await call<WireSession[]>("list")).map(session); },
    async getSessionCommand(id: string, commandId: string) { return command(await call<WireCommand>("command", { session_id: id, command_id: commandId })); },
    async executeSessionCommand(id: string, request: { command: string; runAsync?: boolean; suppressInputEcho?: boolean }, timeout?: number): Promise<Result> {
      let current = await call<WireCommand>("execute", { session_id: id, command: request.command, suppress_input_echo: request.suppressInputEcho ?? false });
      if (request.runAsync) return { cmdId: current.id, output: "", stdout: "", stderr: "" };
      const deadline = timeout ? Date.now() + timeout * 1000 : Infinity;
      while (current.exit_code === null) {
        if (Date.now() >= deadline) throw new DaytonaConnectionTimeoutError("Timed out waiting for session command; command continues running");
        await delay(20);
        current = await call<WireCommand>("command", { session_id: id, command_id: current.id });
      }
      return { cmdId: current.id, exitCode: current.exit_code, ...await logs(id, current.id) };
    },
    getSessionCommandLogs(id: string, commandId: string, onStdout?: Handler, onStderr?: Handler): Promise<Logs | void> {
      return onStdout && onStderr ? follow(id, commandId, onStdout, onStderr) : logs(id, commandId);
    },
    async sendSessionCommandInput(id: string, commandId: string, data: string) {
      const bytes = Buffer.from(data);
      let offset = 0;
      while (offset < bytes.length) {
        const response = await call<{ written: number }>("input", { session_id: id, command_id: commandId, data: bytes.subarray(offset, offset + 32768).toString("base64") });
        offset += response.written;
        if (!response.written) await delay(20);
      }
    },
    async deleteSession(id: string) {
      const current = (await processes.list({ includeOutput: false })).filter(p => p.name.startsWith(prefix) && JSON.parse(p.command).id === id);
      await call("delete", { session_id: id });
      for (const process of current) await processes.kill(process.name);
    },
  };
  return api as SessionAPI;
}
