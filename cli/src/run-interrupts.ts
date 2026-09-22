import { ArkerError } from "@arker-ai/sdk";
import type { EventEmitter } from "node:events";

interface RunControl {
  signalRun(runId: string, options?: { abortSignal?: AbortSignal }): Promise<unknown>;
  cancelRun(runId: string, options?: { abortSignal?: AbortSignal }): Promise<unknown>;
}

/** Keep interrupt requests tied to one acknowledged run, including during dispatch. */
export class RunInterrupts {
  private runId?: string;
  private count = 0;
  private closed = false;
  private terminal = false;
  private readonly pending = new Set<Promise<void>>();
  private interruptInFlight = false;
  private cancelInFlight = false;
  private interruptDone = false;
  private cancelDone = false;
  private retryInterrupt = false;
  private retryCancel = false;
  private readonly interruptRequest = new AbortController();
  private readonly cancelRequest = new AbortController();
  private readonly discoveryRequest = new AbortController();
  private discovering = false;

  constructor(
    private readonly vm: RunControl,
    private readonly signals: Pick<EventEmitter, "on" | "removeListener">,
    private readonly warn: (message: string) => void,
    private readonly discover?: (signal: AbortSignal) => Promise<string>,
  ) {
    signals.on("SIGINT", this.interrupt);
  }

  private readonly interrupt = (): void => {
    if (this.closed) return;
    this.count += 1;
    if (!this.runId && this.discover && !this.discovering) {
      this.discovering = true;
      const discovery = this.discover(this.discoveryRequest.signal).then(
        (id) => this.started(id),
        (error: unknown) => { if (!this.discoveryRequest.signal.aborted) this.warn(`could not find the pending run: ${String(error)}`); },
      );
      this.pending.add(discovery);
      void discovery.then(() => this.pending.delete(discovery), () => this.pending.delete(discovery));
    }
    this.flush();
  };

  started(runId: string): void {
    this.runId = runId;
    this.flush();
  }

  get requested(): boolean { return this.count > 0; }

  observed(state: string): void {
    if (state !== "running" && state !== "pending") {
      this.terminal = true;
      this.interruptRequest.abort();
      this.cancelRequest.abort();
      this.discoveryRequest.abort();
      this.retryInterrupt = this.retryCancel = false;
      return;
    }
    if (this.retryInterrupt || this.retryCancel) this.flush(true);
  }

  private flush(retry = false): void {
    if (this.closed || this.terminal || !this.runId) return;
    if (this.count >= 1 && !this.interruptInFlight && !this.interruptDone && (!retry || this.retryInterrupt)) {
      this.send(false);
    }
    if (this.count >= 2 && !this.cancelInFlight && !this.cancelDone && (!retry || this.retryCancel)) {
      this.send(true);
    }
  }

  private send(force: boolean): void {
    const runId = this.runId!;
    if (force) { this.cancelInFlight = true; this.retryCancel = false; }
    else { this.interruptInFlight = true; this.retryInterrupt = false; }
    const abortSignal = force ? this.cancelRequest.signal : this.interruptRequest.signal;
    const task = (async () => {
      try {
        if (force) await this.vm.cancelRun(runId, { abortSignal });
        else await this.vm.signalRun(runId, { abortSignal });
        if (force) { this.interruptRequest.abort(); this.cancelDone = true; this.interruptDone = true; this.retryInterrupt = false; }
        else this.interruptDone = true;
      } catch (error) {
        if (abortSignal.aborted) return;
        // Admission and guest registration can follow the run acknowledgement.
        // The API fences retries by run identity and signal delivery nonce.
        const retryable = error instanceof ArkerError && error.code === "unavailable";
        if (force) this.retryCancel = retryable;
        else this.retryInterrupt = retryable;
        if (!retryable) {
          if (force) this.cancelDone = true; else this.interruptDone = true;
          this.warn(`could not ${force ? "cancel" : "interrupt"} run ${runId}: ${error instanceof Error ? error.message : String(error)}`);
        }
      } finally {
        if (force) this.cancelInFlight = false;
        else this.interruptInFlight = false;
      }
    })();
    this.pending.add(task);
    void task.then(() => this.pending.delete(task), () => this.pending.delete(task));
  }

  async close(): Promise<void> {
    this.closed = true;
    this.signals.removeListener("SIGINT", this.interrupt);
    this.discoveryRequest.abort();
    this.interruptRequest.abort();
    this.cancelRequest.abort();
    while (this.pending.size) await Promise.allSettled([...this.pending]);
  }
}
