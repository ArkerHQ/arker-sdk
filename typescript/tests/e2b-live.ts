// Live tests for the E2B compatibility layer: common E2B code, unchanged except
// for its imports, against a real deployment. Run with ARKER_API_KEY set.
import assert from "node:assert/strict";

import { Arker } from "../src/index.js";
import { reachable } from "./helpers/reachable.js";
import {
  ALL_TRAFFIC,
  CommandExitError,
  CommandExitError as ExitError,
  FileNotFoundError,
  FilesystemEventType,
  FileType,
  InvalidArgumentError,
  NotFoundError,
  NotSupportedError,
  Sandbox,
  GitAuthError,
  TemplateError,
  TimeoutError,
  Volume,
  VolumeNotFoundError,
} from "../src/e2b.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP e2b live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchUntil(url: string, body: string): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt++) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(10_000) });
      if (response.ok && (await response.text()) === body) return;
    } catch {
      // not serving yet
    }
    await sleep(1_000);
  }
  assert.fail(`${url} never served`);
}

const sbx = await Sandbox.create({ envs: { SANDBOX_VAR: "from-create" }, metadata: { suite: "e2e" } });
try {
  // is an Arker VM
  assert.equal((await new Arker().getVm(sbx.sandboxId)).id, sbx.sandboxId);
  assert.equal((await sbx.arker.run("echo native")).stdout, "native\n");

  // what is missing says so, and how to go on
  assert.throws(
    () => (sbx as unknown as { getMcpUrl(): string }).getMcpUrl(),
    (error: Error) => error instanceof NotSupportedError && /^Sandbox\.getMcpUrl is not supported[\s\S]*\.arker/.test(error.message),
  );

  // default image has python and node
  const tools = await sbx.commands.run("pip --version && node --version && pwd");
  assert.ok(tools.stdout.trim().endsWith("/home/user"));

  // run options
  const seen: string[] = [];
  const result = await sbx.commands.run("echo $SANDBOX_VAR $CMD_VAR $(pwd)", {
    envs: { CMD_VAR: "from-run" },
    cwd: "/tmp",
    onStdout: (data) => void seen.push(data),
  });
  assert.equal(result.stdout, "from-create from-run /tmp\n");
  assert.equal(seen.join(""), result.stdout);
  assert.equal((await sbx.commands.run("pwd")).stdout, "/home/user\n");

  // failures throw like E2B
  const exit = await sbx.commands.run("echo oops >&2; exit 3").catch((error) => error);
  assert.ok(exit instanceof CommandExitError);
  assert.equal(exit.exitCode, 3);
  assert.equal(exit.stderr, "oops\n");
  await assert.rejects(sbx.commands.run("sleep 5", { timeoutMs: 1_000 }), TimeoutError);

  // background command
  const handle = await sbx.commands.run("sleep 1; echo bg-done", { background: true });
  assert.equal((await sbx.commands.run("echo meanwhile")).stdout, "meanwhile\n");
  assert.equal((await handle.wait()).stdout, "bg-done\n");
  const sleeper = await sbx.commands.run("sleep 600", { background: true, timeoutMs: 0 });
  assert.ok(await sleeper.kill());

  // files
  const info = await sbx.files.write("project/main.py", "print('hi')\n");
  assert.equal(info.path, "/home/user/project/main.py");
  await sbx.files.writeFiles([{ path: "/tmp/e2b-a.bin", data: new Uint8Array([0, 1]) }]);
  assert.deepEqual(await sbx.files.read("/tmp/e2b-a.bin", { format: "bytes" }), new Uint8Array([0, 1]));
  assert.equal((await sbx.commands.run("python3 project/main.py")).stdout, "hi\n");
  const entries = Object.fromEntries((await sbx.files.list("/home/user")).map((entry) => [entry.name, entry.type]));
  assert.equal(entries.project, FileType.DIR);
  assert.equal(entries["main.py"], undefined, "list() defaults to one level deep");
  assert.equal(await sbx.files.makeDir("/tmp/e2b-dir"), true);
  assert.equal(await sbx.files.makeDir("/tmp/e2b-dir"), false);
  await sbx.files.rename("/tmp/e2b-dir", "/tmp/e2b-moved");
  assert.ok(await sbx.files.exists("/tmp/e2b-moved"));
  await sbx.files.remove("/tmp/e2b-moved");
  assert.ok(!(await sbx.files.exists("/tmp/e2b-moved")));
  await assert.rejects(sbx.files.read("/tmp/e2b-missing"), FileNotFoundError);

  // getHost serves a port
  await sbx.files.write("/tmp/www/index.html", "served");
  await sbx.commands.run("cd /tmp/www && python3 -m http.server 8000", { background: true, timeoutMs: 0 });
  const host = sbx.getHost(8000);
  await sbx.commands.run("true");
  await fetchUntil(`https://${host}/index.html`, "served");

  // info, reconnect and lifecycle
  const sandboxInfo = await sbx.getInfo();
  assert.deepEqual(sandboxInfo.metadata, { suite: "e2e" });
  assert.ok(sandboxInfo.templateId);
  const again = await Sandbox.connect(sbx.sandboxId, { timeoutMs: 600_000 });
  assert.equal((await again.commands.run("echo $SANDBOX_VAR")).stdout, "from-create\n");
  assert.ok(await again.isRunning());
  assert.ok(await again.pause());

  // E2B doc patterns
  assert.equal((await sbx.commands.run("echo $E2B_SANDBOX_ID")).stdout, `${sbx.sandboxId}\n`);
  assert.equal((await sbx.connect()).sandboxId, sbx.sandboxId);
  const listed = await Sandbox.list({ query: { metadata: { suite: "e2e" } } }).nextItems();
  assert.ok(listed.some((info) => info.sandboxId === sbx.sandboxId));

  // two ports
  assert.notEqual(sbx.getHost(3000), sbx.getHost(3001));
  await sbx.commands.run("true");

  // every reader sees all output, multibyte characters intact
  const text = "é€😀".repeat(400);
  const multi = await sbx.commands.run(`echo out1; printf '${text}'; echo err1 >&2; sleep 1; echo out2`, { background: true });
  const [first, second] = await Promise.all([multi.wait(), multi.wait()]);
  assert.equal(first.stdout, `out1\n${text}out2\n`);
  assert.equal(second.stdout, first.stdout);
  assert.equal(multi.stdout, first.stdout);
  const streamed: string[] = [];
  const stream = await sbx.commands.run(`printf '${text}'; sleep 1; printf '${text}'; echo end`, {
    background: true,
    onStdout: (data) => void streamed.push(data),
  });
  await stream.wait();
  assert.equal(streamed.join(""), `${text}${text}end\n`);

  // background commands leave no sessions, and kill stops the process
  const vm = await new Arker().getVm(sbx.sandboxId);
  const before = (await vm.listSessions()).sessions.length;
  for (let i = 0; i < 3; i++) await (await sbx.commands.run("true", { background: true })).wait();
  const sleeper2 = await sbx.commands.run("sleep 300", { background: true, timeoutMs: 0 });
  assert.ok(await sleeper2.kill());
  assert.equal((await vm.listSessions()).sessions.length, before);
  assert.equal((await sbx.commands.run("pgrep -x sleep || true")).stdout, "");

  // processes by pid
  const byPid = await sbx.commands.run("sleep 600", { background: true, cwd: "/tmp", envs: { TAG: "listed" }, timeoutMs: 0 });
  assert.equal((await sbx.commands.run(`cat /proc/${byPid.pid}/cmdline`)).exitCode, 0);
  const running = (await sbx.commands.list()).find((process) => process.pid === byPid.pid);
  assert.deepEqual([running?.args, running?.cwd, running?.envs], [["-l", "-c", "sleep 600"], "/tmp", { TAG: "listed" }]);
  assert.equal(await sbx.commands.kill(byPid.pid), true);
  assert.ok(!(await sbx.commands.list()).some((process) => process.pid === byPid.pid));
  assert.equal(await sbx.commands.kill(byPid.pid), false);

  // connect to a running command
  const delivered: string[] = [];
  const quiet = await sbx.commands.run("echo hidden", { background: true, onStdout: (data) => void delivered.push(data) });
  await quiet.disconnect();
  await quiet.wait();
  assert.deepEqual(delivered, []);
  const early = await sbx.commands.run("echo early; sleep 2; echo late", { background: true });
  await early.disconnect();
  const reconnected = await (await Sandbox.connect(sbx.sandboxId)).commands.connect(early.pid);
  assert.equal((await reconnected.wait()).stdout, "early\nlate\n");
  await assert.rejects(sbx.commands.connect(early.pid), NotFoundError);

  // stdin
  const reader = await sbx.commands.run("read name; echo hello $name; cat", { background: true, stdin: true });
  await reader.sendStdin("arker\n");
  await sbx.commands.sendStdin(reader.pid, new TextEncoder().encode("line 2\n"));
  const input = sbx.commands.streamInput(reader.pid);
  await input.send("line ");
  await input.send("3\n");
  await input.close();
  await sbx.commands.closeStdin(reader.pid);
  assert.equal((await reader.wait()).stdout, "hello arker\nline 2\nline 3\n");
  const noStdin = await sbx.commands.run("sleep 600", { background: true, timeoutMs: 0 });
  await assert.rejects(noStdin.sendStdin("x"), (error: Error) => error instanceof InvalidArgumentError && /stdin/.test(error.message));
  await noStdin.kill();

  // pty
  const output: Uint8Array[] = [];
  const terminal = await sbx.pty.create({ cols: 80, rows: 24, cwd: "/tmp", envs: { GREETING: "hi" }, onData: (data) => void output.push(data) });
  assert.ok((await sbx.commands.list()).some((process) => process.pid === terminal.pid));
  await sbx.pty.resize(terminal.pid, { cols: 123, rows: 30 });
  await sbx.pty.sendInput(terminal.pid, new TextEncoder().encode("echo $GREETING $(pwd) $TERM $(tput cols); exit 3\n"));
  const exited = await terminal.wait().catch((error) => error);
  assert.ok(exited instanceof ExitError);
  assert.equal(exited.exitCode, 3);
  assert.ok(Buffer.concat(output).toString().includes("hi /tmp xterm-256color 123"));
  const other = await sbx.pty.create({ cols: 80, rows: 24, onData: () => undefined });
  assert.equal(await sbx.pty.kill(other.pid), true);
  assert.ok(!(await sbx.commands.list()).some((process) => process.pid === other.pid));

  // file info and options
  await sbx.files.write("info/a.txt", "12345", { requestTimeoutMs: 5_000, useOctetStream: true });
  await sbx.commands.run("chmod 640 info/a.txt");
  const fileInfo = await sbx.files.getInfo("info/a.txt");
  assert.deepEqual(
    [fileInfo.name, fileInfo.type, fileInfo.size, fileInfo.permissions, fileInfo.mode, fileInfo.owner],
    ["a.txt", FileType.FILE, 5, "rw-r-----", 0o640, "root"],
  );
  assert.equal((await sbx.files.getInfo("info")).type, FileType.DIR);
  await assert.rejects(sbx.files.getInfo("info/missing"), FileNotFoundError);
  const warnings: string[] = [];
  const warn = console.warn;
  console.warn = (message: string) => void warnings.push(message);
  try {
    assert.equal(await sbx.files.read("info/a.txt", { user: "user" }), "12345");
  } finally {
    console.warn = warn;
  }
  assert.ok(warnings.some((message) => /user/.test(message)));
  await assert.rejects(sbx.files.write("info/b.txt", "x", { metadata: { k: "v" } }), (error: Error) => error instanceof NotSupportedError && /metadata/.test(error.message));

  // watchDir
  await sbx.files.write("watched/old.txt", "old");
  const events: [string, FilesystemEventType][] = [];
  let exitedWatch = false;
  const watcher = await sbx.files.watchDir("watched", (event) => void events.push([event.name, event.type]), {
    recursive: true,
    onExit: () => void (exitedWatch = true),
  });
  await sleep(1_500);
  await sbx.files.write("watched/new/inner.txt", "new");
  await sbx.files.write("watched/old.txt", "changed");
  await sbx.files.remove("watched/old.txt");
  await sbx.files.write("watched/old.txt", "back");
  for (let i = 0; i < 20 && events.length < 3; i++) await sleep(500);
  await watcher.stop();
  for (let i = 0; i < 20 && !exitedWatch; i++) await sleep(500);
  assert.ok(exitedWatch);
  assert.ok(events.some(([name, type]) => name === "new" && type === FilesystemEventType.CREATE));
  assert.ok(events.some(([name, type]) => name === "new/inner.txt" && type === FilesystemEventType.CREATE));
  assert.ok(events.some(([name, type]) => name === "old.txt" && type === FilesystemEventType.WRITE));
} finally {
  await sbx.kill();
}

// kill by id
const byId = await Sandbox.create();
assert.equal(await Sandbox.kill(byId.sandboxId), true);
assert.equal(await byId.isRunning(), false);
assert.equal(await byId.kill(), false);

// reconnect keeps pause-on-timeout
const paused = await Sandbox.create({ lifecycle: { onTimeout: "pause" }, timeoutMs: 600_000 });
try {
  await Sandbox.connect(paused.sandboxId, { timeoutMs: 600_000 });
  const vm = (await (await new Arker().getVm(paused.sandboxId)).refresh()) as unknown as {
    policies?: { rules?: Record<string, { action?: unknown }> };
  };
  assert.equal(vm.policies?.rules?.["compat-timeout"]?.action, "suspend", "reconnecting turned pause-on-timeout into delete");
} finally {
  await paused.kill();
}

// timeout deletes the sandbox
const short = await Sandbox.create({ timeoutMs: 5_000 });
const deadline = Date.now() + 120_000;
while (await short.isRunning()) {
  assert.ok(Date.now() < deadline, "sandbox outlived its timeout");
  await sleep(5_000);
}

// an unknown template is a clear error
await assert.rejects(Sandbox.create("my-custom-e2b-template"), (error: Error) => {
  assert.ok(error instanceof TemplateError);
  assert.match(error.message, /No Arker VM named/);
  return true;
});

// create options
const limited = await Sandbox.create({ cpuCount: 4, allowInternetAccess: false });
try {
  assert.equal((await limited.commands.run("nproc")).stdout, "4\n");
  await assert.rejects(limited.commands.run("curl -sS -m 10 https://example.com"), CommandExitError);
} finally {
  await limited.kill();
}

// a command running across a pause finishes
const acrossPause = await Sandbox.create();
try {
  const handle = await acrossPause.commands.run("sleep 3; echo done", { background: true });
  assert.ok(await acrossPause.pause());
  assert.equal((await handle.wait()).stdout, "done\n");
} finally {
  await acrossPause.kill();
}

// network allow and deny lists
const urls = ["https://example.com", "https://www.example.com", "https://github.com", "https://1.1.1.1", "https://8.8.8.8"];
const allowing = await Sandbox.create({ timeoutMs: 120_000, network: { allowOut: ["example.com", "1.1.1.1"], denyOut: [ALL_TRAFFIC] } });
try {
  // Allowed names resolve with no nameserver allowed, and none is.
  assert.deepEqual(await reachable(async (cmd) => (await allowing.commands.run(cmd)).stdout, ...urls), {
    "https://example.com": true,
    "https://www.example.com": false,
    "https://github.com": false,
    "https://1.1.1.1": true,
    "https://8.8.8.8": false,
  });
} finally {
  await allowing.kill();
}
// an allow beats a deny, and what neither names stays reachable
const precedence = await Sandbox.create({ timeoutMs: 120_000, network: { allowOut: ["1.1.1.1"], denyOut: () => ["1.0.0.0/8"] } });
try {
  assert.deepEqual(
    await reachable(async (cmd) => (await precedence.commands.run(cmd)).stdout, "https://1.1.1.1", "https://1.0.0.1", "https://example.com"),
    { "https://1.1.1.1": true, "https://1.0.0.1": false, "https://example.com": true },
  );
} finally {
  await precedence.kill();
}
await assert.rejects(Sandbox.create({ network: { allowOut: ["example.com"] } }), InvalidArgumentError);

// git
const repo = await Sandbox.create({ timeoutMs: 600_000 });
try {
  const git = repo.git;
  await repo.commands.run("git init --bare --initial-branch main /tmp/origin.git");
  await git.init("repo", { initialBranch: "main" });
  await git.configureUser("Arker E2E", "e2e@arker.ai");
  assert.equal(await git.getConfig("user.name"), "Arker E2E");
  await repo.files.write("repo/a.txt", "one");
  const status = await git.status("repo");
  assert.deepEqual([status.currentBranch, status.hasUntracked, status.fileStatus[0]!.name], ["main", true, "a.txt"]);
  await git.add("repo");
  assert.equal((await git.status("repo")).stagedCount, 1);
  await git.commit("repo", "first");
  assert.ok((await git.status("repo")).isClean);
  await git.remoteAdd("repo", "origin", "/tmp/origin.git");
  assert.equal(await git.remoteGet("repo", "origin"), "/tmp/origin.git");
  await git.push("repo");
  assert.equal((await git.status("repo")).upstream, "origin/main");
  await git.createBranch("repo", "feature");
  assert.deepEqual(await git.branches("repo"), { branches: ["feature", "main"], currentBranch: "feature" });
  await git.checkoutBranch("repo", "main");
  await git.deleteBranch("repo", "feature");
  assert.deepEqual((await git.branches("repo")).branches, ["main"]);

  await git.clone("/tmp/origin.git", { path: "copy" });
  await repo.files.write("copy/b.txt", "two");
  await git.add("copy", { files: ["b.txt"] });
  await git.commit("copy", "second", { authorName: "Other", authorEmail: "other@arker.ai" });
  await git.push("copy");
  await git.pull("repo");
  assert.equal(await repo.files.read("repo/b.txt"), "two");
  await repo.files.write("repo/a.txt", "changed");
  assert.equal((await git.status("repo")).fileStatus[0]!.status, "modified");
  await git.restore("repo", { paths: ["a.txt"] });
  assert.equal(await repo.files.read("repo/a.txt"), "one");
  await git.reset("repo", { mode: "hard", target: "HEAD~1" });
  assert.equal(await repo.files.exists("repo/b.txt"), false);
  await assert.rejects(git.checkoutBranch("repo", "missing"), CommandExitError);
  await assert.rejects(git.checkoutBranch("repo", "--orphan=x"), (error: Error) => error instanceof InvalidArgumentError && /starts with '-'/.test(error.message));
  await repo.commands.run("cd repo && git mv a.txt 'new näme.txt' && touch 'tab\there.txt'");
  assert.deepEqual(
    (await git.status("repo")).fileStatus.map((change) => [change.name, change.status, change.renamedFrom]),
    [
      ["new näme.txt", "renamed", "a.txt"],
      ["tab\there.txt", "untracked", undefined],
    ],
  );

  // git credentials
  const privateRepo = "https://github.com/ArkerHQ/no-such-repository-for-e2e";
  await assert.rejects(git.clone(privateRepo, { path: "/tmp/private" }), (error: Error) => error instanceof GitAuthError && /terminal prompts disabled/.test(error.message));
  // Given credentials, git uses them rather than asking for them.
  await assert.rejects(
    git.clone(privateRepo, { path: "/tmp/private", username: "someone", password: "not-a-token" }),
    (error: Error) => error instanceof GitAuthError && !/terminal prompts disabled/.test(error.message),
  );
  await git.dangerouslyAuthenticate({ username: "someone", password: "not-a-token", host: "example.com" });
  assert.equal((await repo.commands.run("cat /root/.git-credentials")).stdout, "https://someone:not-a-token@example.com\n");
} finally {
  await repo.kill();
}

// volume mounts
const volumeName = `e2e-${crypto.randomUUID().slice(0, 12)}`;
const volume = await Volume.create(volumeName);
try {
  const writer = await Sandbox.create({ volumeMounts: { "/mnt/data": volume }, timeoutMs: 120_000 });
  try {
    await writer.files.write("/mnt/data/note.txt", "shared");
  } finally {
    await writer.kill();
  }
  const reader = await Sandbox.create({ volumeMounts: { "/data": volumeName }, timeoutMs: 120_000 });
  try {
    assert.equal(await reader.files.read("/data/note.txt"), "shared");
  } finally {
    await reader.kill();
  }
  assert.ok((await Volume.list()).some((info) => info.volumeId === volume.volumeId && info.name === volumeName));
  assert.equal((await Volume.getInfo(volume.volumeId)).name, volumeName);
  assert.throws(
    () => (volume as unknown as { readFile(path: string): void }).readFile("/note.txt"),
    (error: Error) => error instanceof NotSupportedError && /^Volume\.readFile is not supported/.test(error.message),
  );
} finally {
  assert.equal(await Volume.destroy(volume.volumeId), true);
}
await assert.rejects(Volume.connect(volume.volumeId), VolumeNotFoundError);

console.log("PASS e2b live");
