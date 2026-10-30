import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { DaytonaError, DaytonaNotFoundError } from "@daytona/sdk";
import { setTimeout as delay } from "node:timers/promises";
import type { SandboxInterface } from "computesdk";
import { GuestProcesses, type GuestProcessResult } from "./guest-process.js";
import { shellQuote } from "./common.js";
import source from "./daytona-pty.generated.js";

const prefix = "daytona-pty:";
type Output = (data: Uint8Array) => void | Promise<void>;
export interface TerminalInfo {
  id: string; active: boolean; createdAt: string; cwd: string; envs: Record<string, string>;
  rows: number; cols: number; lazyStart: boolean;
}
export interface TerminalHandle {
  readonly sessionId: string;
  readonly exitCode: number | undefined;
  readonly error: string | undefined;
  isConnected(): boolean;
  waitForConnection(): Promise<void>;
  sendInput(data: string | Uint8Array): Promise<void>;
  resize(cols: number, rows: number): Promise<TerminalInfo>;
  kill(): Promise<void>;
  disconnect(): Promise<void>;
  wait(): Promise<{ exitCode?: number; error?: string }>;
}
export type CreateTerminal = { id: string; cwd?: string; envs?: Record<string, string>; rows?: number; cols?: number; onData: Output };
export interface TerminalAPI {
  createPty(options?: CreateTerminal): Promise<TerminalHandle>;
  connectPty(id: string, options?: { onData: Output }): Promise<TerminalHandle>;
  listPtySessions(): Promise<TerminalInfo[]>;
  getPtySessionInfo(id: string): Promise<TerminalInfo>;
  resizePtySession(id: string, cols: number, rows: number): Promise<TerminalInfo>;
  killPtySession(id: string): Promise<void>;
}

function size(rows: number, cols: number) {
  if (![rows, cols].every(n => Number.isInteger(n) && n >= 0 && n <= 65535)) {
    throw new TypeError("PTY rows and cols must be integers between 0 and 65535");
  }
  return { rows, cols };
}
function info(process: GuestProcessResult): TerminalInfo {
  const metadata = JSON.parse(process.command) as { id: string; cwd: string; envs: Record<string, string> };
  if (!process.tty) throw new DaytonaError("Process has no terminal");
  return { ...metadata, active: process.status === "running",
    createdAt: process.startedAt, ...process.tty, lazyStart: false };
}
class Handle implements TerminalHandle {
  exitCode: number | undefined;
  error: string | undefined;
  private connected = true;
  private readonly reader: Promise<void>;
  private readonly callbackContext = new AsyncLocalStorage<boolean>();
  constructor(readonly sessionId: string, private readonly processes: GuestProcesses,
    private readonly api: TerminalAPI, private readonly identifier: string, onData?: Output, private offset = 0) {
    this.reader = this.read(onData);
  }
  isConnected() { return this.connected; }
  async waitForConnection() { if (!this.connected) throw new DaytonaError("PTY is not connected"); }
  async sendInput(data: string | Uint8Array) {
    if (!this.connected) throw new DaytonaError("PTY is not connected");
    await this.processes.writeStdin(this.identifier, data);
  }
  resize(cols: number, rows: number) { return this.api.resizePtySession(this.sessionId, cols, rows); }
  kill() { return this.api.killPtySession(this.sessionId); }
  async disconnect() { this.connected = false; if (!this.callbackContext.getStore()) await this.reader; }
  async wait() { await this.reader; return { exitCode: this.exitCode, error: this.error }; }
  private async read(onData?: Output) {
    try {
      while (this.connected) {
        const chunk = await this.processes.readOutput(this.identifier, "stdout", this.offset);
        this.offset = chunk.nextOffset;
        if (chunk.data.length && onData) await this.callbackContext.run(true, () => onData(chunk.data));
        if (chunk.eof) {
          this.exitCode = chunk.exitCode ?? undefined;
          this.error = this.exitCode ? "non-zero exit" : undefined;
          this.connected = false;
        } else if (!chunk.data.length) await delay(20);
      }
    } catch (error) {
      this.error = error instanceof Error ? error.message : String(error);
      this.connected = false;
    }
  }
}

export function createDaytonaTerminals(box: SandboxInterface): TerminalAPI {
  const processes = new GuestProcesses(box);
  const current = async (id: string) => {
    const process = (await processes.list({ includeOutput: false })).find(p =>
      p.name.startsWith(prefix) && p.status === "running" && JSON.parse(p.command).id === id);
    if (!process) throw new DaytonaNotFoundError("PTY session not found");
    return process;
  };
  const api: TerminalAPI = {
    async createPty(options) {
      if (!options || typeof options.id !== "string" || !options.id) throw new TypeError("PTY session ID is required");
      const tty = size(options.rows ?? 24, options.cols ?? 80);
      const account = await box.runCommand(`python3 -I -c ${shellQuote(source)} account`);
      if (account.exitCode !== 0) throw new DaytonaError("Unable to resolve terminal account");
      const [home, shell] = JSON.parse(account.stdout) as [string, string];
      const cwd = options.cwd || home;
      const started = await processes.exec({ name: prefix + randomUUID(), exclusiveName: prefix + options.id,
        command: JSON.stringify({ id: options.id, cwd, envs: { TERM: "xterm-256color", ...options.envs } }),
        argv: [shell || "/bin/sh", "-i"], user: "daytona", workingDir: cwd, stdin: true, tty, preserveDescendants: true, ttyOutput: "writers",
        env: { HOME: home, USER: "daytona", LOGNAME: "daytona", TERM: "xterm-256color", ...options.envs } });
      return new Handle(options.id, processes, api, started.name, options.onData);
    },
    async connectPty(id, options) {
      const process = await current(id);
      const stream = await processes.readOutput(process.name, "stdout", 0);
      return new Handle(id, processes, api, process.name, options?.onData, stream.size);
    },
    async getPtySessionInfo(id) { return info(await current(id)); },
    async listPtySessions() { return (await processes.list({ includeOutput: false })).filter(p => p.name.startsWith(prefix) && p.status === "running").map(info); },
    async killPtySession(id) { await processes.kill((await current(id)).name); },
    async resizePtySession(id, cols, rows) {
      const tty = size(rows, cols);
      await processes.resize((await current(id)).name, rows, cols);
      return { ...await api.getPtySessionInfo(id), ...tty };
    },
  };
  return api;
}
