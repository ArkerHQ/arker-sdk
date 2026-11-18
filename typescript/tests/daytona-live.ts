// Live tests for the Daytona compatibility layer: common Daytona code,
// unchanged except for its imports, against a real deployment. Run with
// ARKER_API_KEY set.
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";

import { Arker } from "../src/index.js";
import { reachable } from "./helpers/reachable.js";
import { Daytona, DaytonaError, DaytonaNotFoundError, DaytonaNotSupportedError, DaytonaTimeoutError } from "../src/daytona.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP daytona live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const daytona = new Daytona();
const sandbox = await daytona.create({ envVars: { SANDBOX_VAR: "from-create" }, public: true });
try {
  // is an Arker VM
  assert.equal((await new Arker().getVm(sandbox.id)).id, sandbox.id);
  assert.equal((await sandbox.arker.run("echo native")).stdout, "native\n");
  assert.throws(
    () => (sandbox as unknown as { computerUse: unknown }).computerUse,
    (error: Error) => error instanceof DaytonaNotSupportedError && /^Sandbox\.computerUse is not supported/.test(error.message),
  );

  // exec
  const response = await sandbox.process.executeCommand("echo $SANDBOX_VAR $RUN_VAR $(pwd); echo oops >&2; exit 3", "/tmp", {
    RUN_VAR: "x",
  });
  assert.equal(response.exitCode, 3);
  assert.equal(response.result, "from-create x /tmp\noops\n");
  await assert.rejects(sandbox.process.executeCommand("sleep 5", undefined, undefined, 1), DaytonaTimeoutError);

  // code run
  const code = await sandbox.process.codeRun("import sys; print(sys.argv[1:])", { argv: ["a", "b"] });
  assert.equal(code.result, "['a', 'b']\n");

  // filesystem
  await sandbox.fs.createFolder("/tmp/daytona", "755");
  await sandbox.fs.uploadFile(Buffer.from("hello"), "/tmp/daytona/a.txt");
  assert.equal((await sandbox.fs.downloadFile("/tmp/daytona/a.txt")).toString(), "hello");
  await sandbox.fs.moveFiles("/tmp/daytona/a.txt", "/tmp/daytona/b.txt");
  assert.deepEqual(
    (await sandbox.fs.listFiles("/tmp/daytona")).map(({ name, isDir, size }) => ({ name, isDir, size })),
    [{ name: "b.txt", isDir: false, size: 5 }],
  );
  await sandbox.fs.deleteFile("/tmp/daytona", true);
  await assert.rejects(sandbox.fs.downloadFile("/tmp/daytona/b.txt"), DaytonaNotFoundError);

  // preview link serves a port
  await sandbox.fs.uploadFile(Buffer.from("served"), "/tmp/www/index.html");
  await sandbox.process.executeCommand("cd /tmp/www && (nohup python3 -m http.server 3000 >/dev/null 2>&1 &)");
  const url = `${(await (await daytona.get(sandbox.id)).getPreviewLink(3000)).url}/index.html`;
  let served = false;
  for (let attempt = 0; attempt < 20 && !served; attempt++) {
    try {
      const page = await fetch(url, { signal: AbortSignal.timeout(10_000) });
      served = page.ok && (await page.text()) === "served";
    } catch {
      // not serving yet
    }
    if (!served) await sleep(1_000);
  }
  assert.ok(served, `${url} never served`);

  // sessions
  const proc = sandbox.process;
  await proc.createSession("work");
  await assert.rejects(proc.createSession("work"), DaytonaError);
  assert.equal((await proc.executeSessionCommand("work", { command: "cd /tmp && export STEP=one" })).exitCode, 0);
  const second = await proc.executeSessionCommand("work", { command: "echo $STEP $(pwd) $SANDBOX_VAR" });
  assert.deepEqual([second.stdout, second.exitCode], ["one /tmp from-create\n", 0]);
  const failed = await proc.executeSessionCommand("work", { command: "echo bad >&2; false" });
  assert.deepEqual([failed.stderr, failed.output, failed.exitCode], ["bad\n", "bad\n", 1]);
  assert.deepEqual(
    (await proc.getSession("work")).commands.map(({ command, exitCode }) => [command, exitCode]),
    [
      ["cd /tmp && export STEP=one", 0],
      ["echo $STEP $(pwd) $SANDBOX_VAR", 0],
      ["echo bad >&2; false", 1],
    ],
  );
  assert.equal((await proc.getSessionCommand("work", second.cmdId)).command, "echo $STEP $(pwd) $SANDBOX_VAR");
  assert.ok((await proc.listSessions()).some((session) => session.sessionId === "work"));
  await proc.deleteSession("work");
  assert.ok(!(await proc.listSessions()).some((session) => session.sessionId === "work"));
  await assert.rejects(proc.getSession("work"), DaytonaNotFoundError);

  // async session command, input and logs
  await proc.createSession("async");
  const started = await proc.executeSessionCommand("async", {
    command: "read name; echo hi $name; echo warn >&2; sleep 1",
    runAsync: true,
  });
  assert.equal(started.exitCode, undefined);
  await proc.sendSessionCommandInput("async", started.cmdId, "arker\n");
  const streamedOut: string[] = [];
  const streamedErr: string[] = [];
  await proc.getSessionCommandLogs("async", started.cmdId, (chunk) => streamedOut.push(chunk), (chunk) => streamedErr.push(chunk));
  assert.deepEqual([streamedOut.join(""), streamedErr.join("")], ["hi arker\n", "warn\n"]);
  const logs = await proc.getSessionCommandLogs("async", started.cmdId);
  assert.deepEqual([logs.stdout, logs.stderr], ["hi arker\n", "warn\n"]);
  assert.equal((await proc.getSessionCommand("async", started.cmdId)).exitCode, 0);
  await assert.rejects(proc.sendSessionCommandInput("async", started.cmdId, "late\n"), DaytonaError);
  await proc.deleteSession("async");

  // pty sessions
  const ptyOutput: Uint8Array[] = [];
  const pty = await proc.createPty({ id: "term", cwd: "/tmp", envs: { GREETING: "hi" }, cols: 80, rows: 24, onData: (data) => void ptyOutput.push(data) });
  await pty.waitForConnection();
  await proc.resizePtySession("term", 111, 30);
  const ptyInfo = await proc.getPtySessionInfo("term");
  assert.deepEqual([ptyInfo.id, ptyInfo.cwd, ptyInfo.envs, ptyInfo.cols, ptyInfo.rows], ["term", "/tmp", { GREETING: "hi" }, 111, 30]);
  assert.deepEqual((await proc.listPtySessions()).map((listed) => listed.id), ["term"]);
  await pty.sendInput("echo $GREETING $(pwd) $(tput cols); exit 4\n");
  assert.equal((await pty.wait()).exitCode, 4);
  assert.ok(Buffer.concat(ptyOutput).toString().includes("hi /tmp 111"));
  const otherPty = await proc.createPty({ id: "other" });
  assert.deepEqual((await proc.listPtySessions()).map((listed) => listed.id), ["other"]);
  await proc.killPtySession("other");
  assert.equal((await otherPty.wait()).exitCode, undefined);
  assert.deepEqual(await proc.listPtySessions(), []);
  await assert.rejects(proc.getPtySessionInfo("other"), DaytonaNotFoundError);
  await assert.rejects(proc.connectPty("other"), DaytonaNotFoundError);

  // filesystem extras
  const fs = sandbox.fs;
  const local = await mkdtemp(join(tmpdir(), "daytona-live-"));
  await writeFile(join(local, "local.txt"), "from disk");
  await fs.uploadFiles([
    { source: Buffer.from("alpha TODO one\n"), destination: "/tmp/fsx/a.py" },
    { source: join(local, "local.txt"), destination: "/tmp/fsx/sub/b.txt" },
  ]);
  await fs.uploadFileStream(Readable.from([Buffer.from("TODO streamed\n")]), "/tmp/fsx/sub/c.py");
  const chunks: Buffer[] = [];
  for await (const chunk of await fs.downloadFileStream("/tmp/fsx/sub/c.py")) chunks.push(chunk);
  assert.equal(Buffer.concat(chunks).toString(), "TODO streamed\n");
  const details = await fs.getFileDetails("/tmp/fsx/a.py");
  assert.deepEqual([details.name, details.isDir, details.size, details.owner], ["a.py", false, 15, "root"]);
  await fs.setFilePermissions("/tmp/fsx/a.py", { mode: "600" });
  const changed = await fs.getFileDetails("/tmp/fsx/a.py");
  assert.deepEqual([changed.permissions, changed.mode], ["0600", "-rw-------"]);
  assert.ok((await fs.getFileDetails("/tmp/fsx/sub")).isDir);
  assert.deepEqual((await fs.listFiles("/tmp/fsx", { depth: 2 })).map((f) => f.name).sort(), ["a.py", "b.txt", "c.py", "sub"]);
  assert.deepEqual((await fs.listFiles("/tmp/fsx")).map((f) => f.name).sort(), ["a.py", "sub"]);
  const matches = (await fs.findFiles("/tmp/fsx", "TODO")).map((m) => [m.file, m.line, m.content]).sort();
  assert.deepEqual(matches, [
    ["/tmp/fsx/a.py", 1, "alpha TODO one"],
    ["/tmp/fsx/sub/c.py", 1, "TODO streamed"],
  ]);
  assert.deepEqual((await fs.searchFiles("/tmp/fsx", "*.py")).files.sort(), ["/tmp/fsx/a.py", "/tmp/fsx/sub/c.py"]);
  const replaced = await fs.replaceInFiles(["/tmp/fsx/a.py", "/tmp/fsx/missing.py"], "TODO", "DONE");
  assert.deepEqual(replaced.map((r) => [r.file, r.success]), [
    ["/tmp/fsx/a.py", true],
    ["/tmp/fsx/missing.py", false],
  ]);
  assert.equal((await fs.downloadFile("/tmp/fsx/a.py")).toString(), "alpha DONE one\n");
  const downloads = await fs.downloadFiles([{ source: "/tmp/fsx/sub/b.txt" }, { source: "/tmp/fsx/nope", destination: join(local, "n") }]);
  assert.equal(downloads[0]!.result?.toString(), "from disk");
  assert.ok(downloads[1]!.error);
  await assert.rejects(fs.getFileDetails("/tmp/fsx/nope"), DaytonaNotFoundError);
} finally {
  await daytona.delete(sandbox);
}

// image, resources and language
const node = await daytona.create({ image: "node:22-slim", language: "typescript", resources: { cpu: 2 } });
try {
  assert.equal((await node.process.executeCommand("nproc")).result, "2\n");
  assert.equal((await node.process.codeRun("const n: number = 2; console.log(n * 21)")).result, "42\n");
} finally {
  await node.delete();
}

// an unknown snapshot is not found
await assert.rejects(daytona.create({ snapshot: "no-such-arker-vm" }), DaytonaNotFoundError);

// a network allow list denies everything else
const allowlisted = await daytona.create({ networkAllowList: "1.1.1.1/32" });
try {
  const run = async (cmd: string) => (await allowlisted.process.executeCommand(cmd)).result;
  assert.deepEqual(await reachable(run, "https://1.1.1.1", "https://example.com"), {
    "https://1.1.1.1": true,
    "https://example.com": false,
  });
} finally {
  await allowlisted.delete();
}

// git
const repo = await daytona.create();
try {
  const git = repo.git;
  await repo.process.executeCommand("git init --bare --initial-branch main /tmp/origin.git");
  await git.init("repo", false, "main");
  await git.configureUser("Arker E2E", "e2e@arker.ai");
  await repo.fs.uploadFile(new TextEncoder().encode("one"), "/root/repo/a.txt");
  const status = await git.status("repo");
  assert.equal(status.currentBranch, "main");
  assert.deepEqual(status.fileStatus.map((file) => [file.name, file.staging, file.worktree]), [["a.txt", "Untracked", "Untracked"]]);
  await git.add("repo", ["a.txt"]);
  assert.equal((await git.status("repo")).fileStatus[0]!.staging, "Added");
  const commit = await git.commit("repo", "first", "Arker E2E", "e2e@arker.ai");
  assert.equal((await repo.process.executeCommand("git -C repo rev-parse HEAD")).result.trim(), commit.sha);
  await git.remoteAdd("repo", "origin", "/tmp/origin.git");
  assert.deepEqual((await git.remotes("repo")).remotes, [{ name: "origin", url: "/tmp/origin.git" }]);
  await git.push("repo", undefined, undefined, undefined, undefined, true);
  assert.equal((await git.status("repo")).branchPublished, true);
  await git.createBranch("repo", "feature");
  assert.deepEqual(await git.branches("repo"), { branches: ["feature", "main"], current: "feature" });
  await git.checkoutBranch("repo", "main");
  await git.deleteBranch("repo", "feature");

  await git.clone("/tmp/origin.git", "copy");
  await repo.fs.uploadFile(new TextEncoder().encode("two"), "/root/copy/b.txt");
  await git.add("copy", ["b.txt"]);
  const second = await git.commit("copy", "second", "Other", "other@arker.ai");
  await git.push("copy");
  await git.pull("repo");
  assert.equal((await repo.fs.downloadFile("/root/repo/b.txt")).toString(), "two");
  await git.clone("/tmp/origin.git", "pinned", undefined, commit.sha);
  assert.equal((await repo.process.executeCommand("git -C pinned rev-parse HEAD")).result.trim(), commit.sha);
  assert.notEqual(commit.sha, second.sha);
  await git.reset("repo", "hard", "HEAD~1");
  assert.equal((await repo.process.executeCommand("test -e repo/b.txt")).exitCode, 1);
  await assert.rejects(git.checkoutBranch("repo", "missing"), DaytonaError);
} finally {
  await repo.delete();
}

// code interpreter
const interpreted = await daytona.create();
try {
  const interpreter = interpreted.codeInterpreter;
  await interpreter.runCode("counter = 40");
  const printed: string[] = [];
  const result = await interpreter.runCode("print(counter + 2)", { onStdout: (message) => void printed.push(message.output) });
  assert.deepEqual([result.stdout, result.error, printed], ["42\n", undefined, ["42\n"]]);
  const failed = await interpreter.runCode("raise ValueError('bad')");
  assert.deepEqual([failed.error?.name, failed.error?.value], ["ValueError", "bad"]);
  const context = await interpreter.createContext("/tmp");
  assert.equal((await interpreter.runCode("import os\nprint('counter' in globals(), os.getcwd())", { context })).stdout, "False /tmp\n");
  assert.deepEqual((await interpreter.listContexts()).map((listed) => listed.id), [context.id]);
  await assert.rejects(interpreter.runCode("import time\ntime.sleep(30)", { context, timeout: 2 }), DaytonaTimeoutError);
  await interpreter.deleteContext(context);
  assert.deepEqual(await interpreter.listContexts(), []);
} finally {
  await interpreted.delete();
}

// volumes
const volume = await daytona.volume.get(`e2e-${crypto.randomUUID().slice(0, 12)}`, true);
try {
  assert.ok((await daytona.volume.list()).some((listed) => listed.id === volume.id));
  const writer = await daytona.create({ volumes: [{ volumeId: volume.id, mountPath: "/mnt/data" }] });
  try {
    await writer.fs.uploadFile(new TextEncoder().encode("shared"), "/mnt/data/note.txt");
  } finally {
    await writer.delete();
  }
  const reader = await daytona.create({ volumes: [{ volumeId: volume.name, mountPath: "/data" }] });
  try {
    assert.equal((await reader.fs.downloadFile("/data/note.txt")).toString(), "shared");
  } finally {
    await reader.delete();
  }
} finally {
  await daytona.volume.delete(volume);
}
await assert.rejects(daytona.volume.delete(volume), DaytonaNotFoundError);

console.log("PASS daytona live");
