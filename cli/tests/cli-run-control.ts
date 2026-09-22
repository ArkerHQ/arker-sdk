import assert from "node:assert/strict";
import { ArkerError } from "@arker-ai/sdk";
import { EventEmitter } from "node:events";
import { RunInterrupts } from "../src/run-interrupts.js";

const signals = new EventEmitter();
const calls: string[] = [];
let releaseInterrupt!: () => void;
const controller = new RunInterrupts({
  signalRun: async (id: string) => { calls.push(`INT:${id}`); await new Promise<void>((resolve) => { releaseInterrupt = resolve; }); },
  cancelRun: async (id: string) => { calls.push(`KILL:${id}`); return true; },
}, signals, (message) => { throw new Error(message); });
signals.emit("SIGINT");
assert.deepEqual(calls, []);
controller.started("run-a");
assert.deepEqual(calls, ["INT:run-a"]);
signals.emit("SIGINT");
await Promise.resolve();
assert.deepEqual(calls, ["INT:run-a", "KILL:run-a"]);
releaseInterrupt();
await controller.close();
assert.equal(signals.listenerCount("SIGINT"), 0);
signals.emit("SIGINT");
assert.deepEqual(calls, ["INT:run-a", "KILL:run-a"]);
console.log("PASS run interrupt ownership and escalation");

const retrySignals = new EventEmitter();
const retryCalls: string[] = [];
const warnings: string[] = [];
let tries = 0;
const retry = new RunInterrupts({
  signalRun: async (id) => { retryCalls.push(`INT:${id}`); if (++tries === 1) throw new ArkerError("unavailable", "run has not started", 503); },
  cancelRun: async (id) => { retryCalls.push(`KILL:${id}`); },
}, retrySignals, (message) => warnings.push(message));
retry.started("run-b");
retrySignals.emit("SIGINT");
await new Promise<void>((resolve) => setImmediate(resolve));
retry.observed("running");
await new Promise<void>((resolve) => setImmediate(resolve));
assert.deepEqual(retryCalls, ["INT:run-b", "INT:run-b"]);
retry.observed("completed");
retrySignals.emit("SIGINT");
await retry.close();
assert.deepEqual(retryCalls, ["INT:run-b", "INT:run-b"]);
assert.deepEqual(warnings, []);
assert.equal(retrySignals.listenerCount("SIGINT"), 0);
console.log("PASS pending interrupt retries and terminal ownership");
