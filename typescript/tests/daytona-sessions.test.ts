import assert from "node:assert/strict";
import { test } from "node:test";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { SandboxInterface } from "computesdk";
import { DaytonaInternalServerError, DaytonaSessionEndedError } from "@daytona/sdk";
import { createDaytonaSessions } from "../src/compat/daytona-sessions.js";
import source from "../src/compat/daytona-session.generated.js";

async function control(payload: Record<string, unknown>) {
  // Execute the shared control implementation directly as the test's existing user.
  const script = `${source.split('if __name__ == "__main__":')[0]}\nprint(json.dumps({"result":call(json.loads(sys.argv[1]))}))`;
  const process = spawn("python3", ["-c", script, JSON.stringify(payload)]);
  let stdout = "", stderr = "";
  process.stdout.on("data", value => { stdout += value; });
  process.stderr.on("data", value => { stderr += value; });
  const code = await new Promise<number | null>((resolve, reject) => { process.on("exit", resolve); process.on("error", reject); });
  if (code !== 0) throw new Error(stderr);
  return { stdout, stderr, exitCode: code };
}

test("session shell state, incremental UTF-8 logs and input use the shared guest protocol", async () => {
  const directory = await mkdtemp(join(tmpdir(), "daytona-session-ts-"));
  const id = "ts-" + randomUUID();
  const child = spawn("python3", ["-c", source, "serve", id], { cwd: directory, detached: true });
  let diagnostic = "";
  child.stderr.on("data", data => { diagnostic += data; });
  const exited = new Promise(resolve => child.on("exit", resolve));
  const box = {
    filesystem: { writeFile: async () => {} },
    runCommand: async (command: string) => {
      const token = command.split(" ").at(-1)!.replaceAll("'", "");
      return control(JSON.parse(Buffer.from(token, "base64").toString()));
    },
  } as unknown as SandboxInterface;
  const api = createDaytonaSessions(box);
  try {
    const deadline = Date.now() + 5000;
    for (;;) {
      try { await api.getSession(id); break; }
      catch (error) { if (Date.now() >= deadline || child.exitCode !== null) throw new Error(diagnostic, { cause: error }); await new Promise(resolve => setTimeout(resolve, 10)); }
    }
    await api.executeSessionCommand(id, { command: "export KEPT=yes; cd /tmp; retained() { printf function; }; printf first" });
    const result = await api.executeSessionCommand(id, { command: "printf '%s:%s:' \"$KEPT\" \"$PWD\"; retained; (exit 7)" });
    assert.equal(result.stdout, "yes:/tmp:function\n");
    assert.equal(result.exitCode, 7);
    const input = await api.executeSessionCommand(id, { command: "read value; printf '%s' \"$value\"; printf error >&2", runAsync: true, suppressInputEcho: true });
    const output: string[] = [], errors: string[] = [];
    const following = api.getSessionCommandLogs(id, input.cmdId, async text => { await Promise.resolve(); output.push(text); }, text => { errors.push(text); });
    await api.sendSessionCommandInput(id, input.cmdId, "hëllo\n");
    await following;
    assert.equal(output.join(""), "hëllo\n");
    assert.equal(errors.join(""), "error\n");
    assert.equal((await api.getSession(id)).commands.length, 3);
    const retained = await api.executeSessionCommand(id, { command: "printf 'ready\\n'; read value; printf done", runAsync: true, suppressInputEcho: true });
    const interruption = new Error("stop local reader");
    await assert.rejects(api.getSessionCommandLogs(id, retained.cmdId, () => { throw interruption; }, () => {}), error => error === interruption);
    assert.equal((await api.getSessionCommand(id, retained.cmdId)).exitCode, undefined);
    await api.sendSessionCommandInput(id, retained.cmdId, "continue\n");
    const continued: string[] = [];
    await api.getSessionCommandLogs(id, retained.cmdId, text => { continued.push(text); }, () => {});
    assert.equal(continued.join(""), "ready\ndone\n");
    const ended = await api.executeSessionCommand(id, {command: "printf before; exit 7", runAsync: true});
    const final: string[] = [];
    const stopEndedReader = new Error("stop ended-session log subscription");
    await assert.rejects(api.getSessionCommandLogs(id, ended.cmdId, text => {final.push(text); throw stopEndedReader;}, () => {}), error => error === stopEndedReader);
    assert.equal(final.join(""), "before\n");
    assert.equal((await api.getSessionCommand(id, ended.cmdId)).exitCode, undefined);
  } finally {
    await control({ action: "delete", session_id: id }).catch(() => undefined);
    await exited;
    try { process.kill(-child.pid!, "SIGKILL"); } catch {}
    await rm(directory, { recursive: true, force: true });
  }
});

test("startup retains primary and cleanup errors with the owned process identity", async () => {
  const { GuestProcesses } = await import("../src/compat/guest-process.js");
  const original = { exec: GuestProcesses.prototype.exec, get: GuestProcesses.prototype.get, kill: GuestProcesses.prototype.kill };
  const primary = new Error("readiness failure"), cleanup = new Error("kill failure");
  const killed: string[] = [];
  GuestProcesses.prototype.exec = async () => ({ name: "owned-process" } as never);
  GuestProcesses.prototype.get = async () => ({ status: "failed" } as never);
  GuestProcesses.prototype.kill = async id => { killed.push(id); throw cleanup; };
  const box = {
    filesystem: { writeFile: async () => {} },
    runCommand: async (command: string) => {
      if (command.includes(" -c ")) return { stdout: '["/tmp"]', stderr: "", exitCode: 0 };
      throw primary;
    },
  } as unknown as SandboxInterface;
  try {
    await assert.rejects(createDaytonaSessions(box).createSession("public-session"), error => error === primary);
    assert.deepEqual(killed, ["owned-process"]);
    assert.equal((primary as Error & { cleanupError: Error }).cleanupError, cleanup);
    assert.equal((primary as Error & { processId: string }).processId, "owned-process");
    assert.equal((primary as Error & { sessionId: string }).sessionId, "public-session");
  } finally {
    Object.assign(GuestProcesses.prototype, original);
  }
});


test("ended session wire error retains native error identity and status", async () => {
  const box = {filesystem: {writeFile: async () => {}}, runCommand: async () => ({stdout: JSON.stringify({error: "session process has exited", code: "session_ended"}), stderr: "", exitCode: 1})} as unknown as SandboxInterface;
  await assert.rejects(createDaytonaSessions(box).executeSessionCommand("ended", {command: "printf after", runAsync: true}), error => error instanceof DaytonaSessionEndedError && error.statusCode === 410);
});


test("closed input keeps native internal error rather than missing resource", async () => {
  const box = {filesystem: {writeFile: async () => {}}, runCommand: async () => ({stdout: JSON.stringify({error: "Command input is closed", code: "input_closed"}), stderr: "", exitCode: 1})} as unknown as SandboxInterface;
  await assert.rejects(createDaytonaSessions(box).sendSessionCommandInput("ended", "command", "after"), error => error instanceof DaytonaInternalServerError && error.statusCode === 500);
});
