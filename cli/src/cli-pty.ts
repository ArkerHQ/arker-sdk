import { stdin as defaultInput, stdout as defaultOutput } from "node:process";
import type { PtyConnection } from "@arker-ai/sdk";

export interface PtyBridgeOptions {
  fallbackCols: number;
  fallbackRows: number;
  autoResize: boolean;
}

interface EventSource {
  on(event: string | symbol, listener: (...args: any[]) => void): unknown;
  off(event: string | symbol, listener: (...args: any[]) => void): unknown;
  once(event: string | symbol, listener: (...args: any[]) => void): unknown;
}

interface PtyInputStream extends EventSource {
  isTTY?: boolean;
  isRaw?: boolean;
  setRawMode?(enabled: boolean): void;
  resume(): unknown;
  pause(): unknown;
}

interface PtyOutputStream {
  columns?: number;
  rows?: number;
  isTTY?: boolean;
  write(data: Uint8Array): unknown;
}

export interface PtyBridgeRuntime {
  input: PtyInputStream;
  output: PtyOutputStream;
  signals: EventSource;
  reportError(message: string): void;
}

const defaultRuntime: PtyBridgeRuntime = {
  input: defaultInput as unknown as PtyInputStream,
  output: defaultOutput as unknown as PtyOutputStream,
  signals: process,
  reportError: (message) => { process.stderr.write(`arker: ${message}\n`); },
};

export function bridgePty(
  pty: PtyConnection,
  options: PtyBridgeOptions,
  runtime: PtyBridgeRuntime = defaultRuntime,
): Promise<number> {
  const { input, output, signals } = runtime;
  return new Promise((resolve) => {
    let settled = false;
    let rawEnabled = false;
    const wasRaw = Boolean(input.isTTY && input.isRaw);
    const restoreTerminal = () => {
      if (rawEnabled && input.isTTY && input.setRawMode) input.setRawMode(wasRaw);
      rawEnabled = false;
    };
    const finish = (code: number) => {
      if (settled) return;
      settled = true;
      restoreTerminal();
      input.off("data", onInput);
      input.off("end", onInputEnd);
      input.pause();
      signals.off("SIGWINCH", onResize);
      signals.off("SIGINT", onSigint);
      signals.off("SIGTERM", onSigterm);
      signals.off("SIGHUP", onSighup);
      signals.off("exit", restoreTerminal);
      offData();
      offClose();
      offError();
      resolve(code);
    };
    const onInput = (chunk: Buffer) => { pty.send(chunk); };
    const onInputEnd = () => { pty.close(1000, "stdin closed"); };
    const onResize = () => {
      pty.resize(output.columns ?? options.fallbackCols, output.rows ?? options.fallbackRows);
    };
    const onSigint = () => { pty.send(new Uint8Array([0x03])); };
    const onSigterm = () => { pty.close(); finish(143); };
    const onSighup = () => { pty.close(); finish(129); };
    const offData = pty.onData((data) => { output.write(data); });
    const offClose = pty.onClose((event) => {
      if (event.code === 1000) {
        finish(0);
        return;
      }
      const reason = event.reason ? `: ${event.reason}` : "";
      runtime.reportError(`pty closed abnormally (${event.code ?? "unknown"})${reason}`);
      finish(1);
    });
    const offError = pty.onError((error) => {
      const message = error instanceof Error ? error.message : String(error);
      runtime.reportError(`pty error: ${message}`);
      finish(1);
    });

    signals.once("exit", restoreTerminal);
    pty.ready
      .then(() => {
        if (settled) return;
        if (input.isTTY && input.setRawMode) {
          input.setRawMode(true);
          rawEnabled = true;
        }
        input.resume();
        input.on("data", onInput);
        input.on("end", onInputEnd);
        if (options.autoResize) signals.on("SIGWINCH", onResize);
        signals.on("SIGINT", onSigint);
        signals.on("SIGTERM", onSigterm);
        signals.on("SIGHUP", onSighup);
        if (options.autoResize) onResize();
      })
      .catch((error) => {
        if (settled) return;
        const message = error instanceof Error ? error.message : String(error);
        runtime.reportError(`pty failed to open: ${message}`);
        finish(1);
      });
  });
}
