// Live tests for the Modal compatibility layer: common Modal sandbox code,
// unchanged except for its imports, against a real deployment. Run with
// ARKER_API_KEY set.
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Arker, ArkerError } from "../src/index.js";
import { reachable } from "./helpers/reachable.js";
import {
  ExecutionError,
  ModalClient,
  NotFoundError,
  NotSupportedError,
  SandboxFilesystemNotFoundError,
  TimeoutError,
  type FileWatchEvent,
} from "../src/modal.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP modal live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const decoder = new TextDecoder();

async function exists(vmId: string): Promise<boolean> {
  try {
    await new Arker().getVm(vmId);
    return true;
  } catch (error) {
    if (error instanceof ArkerError && error.status === 404) return false;
    throw error;
  }
}

const modal = new ModalClient();
const app = await modal.apps.fromName("arker-e2e", { createIfMissing: true });
const sb = await modal.sandboxes.create(app, undefined, {
  env: { SANDBOX_VAR: "from-create" },
  secrets: [await modal.secrets.fromObject({ TOKEN: "secret" })],
  encryptedPorts: [8000],
  workdir: "/tmp",
});
try {
  // is an Arker VM
  assert.equal((await new Arker().getVm(sb.sandboxId)).id, sb.sandboxId);
  assert.equal((await sb.arker.run("echo native")).stdout, "native\n");

  // exec
  const proc = await sb.exec(["bash", "-c", "echo $SANDBOX_VAR $TOKEN $RUN_VAR $(pwd); echo oops >&2; exit 3"], {
    env: { RUN_VAR: "x" },
  });
  assert.equal(await proc.wait(), 3);
  assert.equal(await proc.poll(), 3);
  assert.equal(await proc.stdout.readText(), "from-create secret x /tmp\n");
  assert.equal(await proc.stderr.readText(), "oops\n");
  assert.equal(await (await sb.exec(["pwd"], { workdir: "/" })).stdout.readText(), "/\n");

  // exec streams and runs concurrently
  const slow = await sb.exec(["bash", "-c", "for i in 1 2 3; do echo line$i; sleep 1; done"]);
  assert.equal(await (await sb.exec(["echo", "meanwhile"])).stdout.readText(), "meanwhile\n");
  const chunks: string[] = [];
  for await (const chunk of slow.stdout as unknown as AsyncIterable<string>) chunks.push(chunk);
  assert.equal(chunks.join(""), "line1\nline2\nline3\n");
  assert.ok(chunks.length > 1, "output arrived in one piece instead of streaming");

  // exec timeout
  await assert.rejects((await sb.exec(["sleep", "5"], { timeoutMs: 1_000 })).wait(), TimeoutError);

  // files
  const notes = await sb.open("/tmp/modal/notes.txt", "w");
  await notes.write("hello");
  await notes.close();
  const appended = await sb.open("/tmp/modal/notes.txt", "a");
  await appended.write(" world");
  await appended.close();
  assert.equal(decoder.decode(await (await sb.open("/tmp/modal/notes.txt")).read()), "hello world");
  await (await sb.exec(["rm", "-rf", "/tmp/modal"])).wait();
  await assert.rejects(sb.open("/tmp/modal/notes.txt"), SandboxFilesystemNotFoundError);

  // tunnel serves a port
  const page = await sb.open("/tmp/www/index.html", "w");
  await page.write("served");
  await page.close();
  await sb.exec(["python3", "-m", "http.server", "8000"], { workdir: "/tmp/www" });
  const url = `${(await (await modal.sandboxes.fromId(sb.sandboxId)).tunnels())[8000]!.url}/index.html`;
  let served = false;
  for (let attempt = 0; attempt < 20 && !served; attempt++) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(10_000) });
      served = response.ok && (await response.text()) === "served";
    } catch {
      // not serving yet
    }
    if (!served) await sleep(1_000);
  }
  assert.ok(served, `${url} never served`);

  // stdout then stderr, and both at once: each stream yields all of its output
  const collect = async (stream: unknown) => {
    const parts: string[] = [];
    for await (const part of stream as AsyncIterable<string>) parts.push(part);
    return parts.join("");
  };
  const both = await sb.exec(["bash", "-c", "echo o; sleep 1; echo e >&2; sleep 1; echo o2"]);
  assert.equal(await collect(both.stdout), "o\no2\n");
  assert.equal(await collect(both.stderr), "e\n");
  assert.equal(await both.stdout.readText(), "o\no2\n");
  const together = await sb.exec(["bash", "-c", "for i in 1 2 3; do echo o$i; echo e$i >&2; sleep 0.6; done"]);
  const [out, err] = await Promise.all([collect(together.stdout), collect(together.stderr)]);
  assert.equal(out, "o1\no2\no3\n");
  assert.equal(err, "e1\ne2\ne3\n");

  // exec stdin
  const reader = await sb.exec(["bash", "-c", "read first; echo got $first; cat"]);
  await reader.stdin.writeText("one\n");
  const writer = reader.stdin.getWriter();
  await writer.write("two\n");
  await writer.close();
  assert.equal(await reader.wait(), 0);
  assert.equal(await reader.stdout.readText(), "got one\ntwo\n");

  // exec stream options
  const quiet = await sb.exec(["bash", "-c", "echo out; echo err >&2"], { stdout: "ignore" });
  assert.equal(await quiet.wait(), 0);
  assert.deepEqual([await quiet.stdout.readText(), await quiet.stderr.readText()], ["", "err\n"]);
  const raw = await sb.exec(["printf", "a\\nb"], { mode: "binary" });
  const rawChunks: Uint8Array[] = [];
  for await (const chunk of raw.stdout as unknown as AsyncIterable<Uint8Array>) rawChunks.push(chunk);
  assert.equal(decoder.decode(Buffer.concat(rawChunks)), "a\nb");
  await assert.rejects(sb.exec(["true"], { pty: true }), (error: Error) => error instanceof NotSupportedError && /pty/.test(error.message));

  // filesystem
  const fs = sb.filesystem;
  await fs.writeText("hello", "/tmp/fs/a.txt");
  await fs.writeBytes(new Uint8Array([0, 1]), "/tmp/fs/b.bin");
  assert.equal(await fs.readText("/tmp/fs/a.txt"), "hello");
  assert.deepEqual(await fs.readBytes("/tmp/fs/b.bin"), new Uint8Array([0, 1]));
  await fs.makeDirectory("/tmp/fs/sub/deeper");
  await assert.rejects(fs.makeDirectory("/tmp/fs/none/deeper", { createParents: false }), ExecutionError);
  const listedFiles = Object.fromEntries((await fs.listFiles("/tmp/fs")).map((info) => [info.name, info]));
  assert.deepEqual(Object.keys(listedFiles).sort(), ["a.txt", "b.bin", "sub"]);
  assert.deepEqual([listedFiles.sub!.type, listedFiles["a.txt"]!.type, listedFiles["a.txt"]!.size], ["directory", "file", 5]);
  await (await sb.exec(["chmod", "600", "/tmp/fs/a.txt"])).wait();
  const stat = await fs.stat("/tmp/fs/a.txt");
  assert.deepEqual([stat.path, stat.permissions, stat.owner], ["/tmp/fs/a.txt", "rw-------", "root"]);
  const local = await mkdtemp(join(tmpdir(), "modal-live-"));
  await writeFile(join(local, "local.txt"), "from local");
  await fs.copyFromLocal(join(local, "local.txt"), "/tmp/fs/copied.txt");
  await fs.copyToLocal("/tmp/fs/copied.txt", join(local, "back", "copied.txt"));
  assert.equal(await readFile(join(local, "back", "copied.txt"), "utf8"), "from local");
  await assert.rejects(fs.remove("/tmp/fs"), ExecutionError);
  await fs.remove("/tmp/fs", { recursive: true });
  await assert.rejects(fs.stat("/tmp/fs"), SandboxFilesystemNotFoundError);
  await assert.rejects(fs.readText("/tmp/fs/a.txt"), SandboxFilesystemNotFoundError);

  // filesystem watch
  await fs.makeDirectory("/tmp/watched");
  await sb.exec(["bash", "-c", "sleep 2; echo x > /tmp/watched/a; sleep 2; rm /tmp/watched/a"]);
  const watched: FileWatchEvent[] = [];
  for await (const event of fs.watch("/tmp/watched", { timeoutMs: 8_000 })) watched.push(event);
  assert.deepEqual(watched, [
    { eventType: "Create", paths: ["/tmp/watched/a"] },
    { eventType: "Remove", paths: ["/tmp/watched/a"] },
  ]);
  await sb.exec(["bash", "-c", "sleep 2; echo y > /tmp/watched/b; sleep 2; rm /tmp/watched/b"]);
  const removes: string[] = [];
  for await (const event of fs.watch("/tmp/watched", { filter: ["Remove"], timeoutMs: 8_000 })) removes.push(event.eventType);
  assert.deepEqual(removes, ["Remove"]);
} finally {
  await sb.terminate();
}

// entrypoint streams
const echo = await modal.sandboxes.create(app, undefined, {
  command: ["bash", "-c", "read line; echo got $line; echo err >&2"],
  timeoutMs: 120_000,
});
try {
  await echo.stdin.writeText("hello\n");
  await echo.stdin.close();
  assert.equal(await echo.wait(), 0);
  assert.equal(await echo.stdout.readText(), "got hello\n");
  assert.equal(await echo.stderr.readText(), "err\n");
} finally {
  await echo.terminate();
}

// image, entrypoint and options
const image = modal.images
  .fromRegistry("python:3.12-slim")
  .dockerfileCommands(["RUN python3 -m pip install six", "ENV FROM_IMAGE=1"]);
const entry = await modal.sandboxes.create(app, image, {
  command: ["python", "-c", "import six, os; print(os.environ['FROM_IMAGE']); raise SystemExit(4)"],
  cpu: 2,
  blockNetwork: true,
});
try {
  assert.equal(await entry.wait(), 4);
  assert.equal(await (await entry.exec(["nproc"])).stdout.readText(), "2\n");
  const fetchPage = "import urllib.request; urllib.request.urlopen('https://example.com', timeout=10)";
  const blocked = await entry.exec(["python", "-c", fetchPage]);
  assert.notEqual(await blocked.wait(), 0);
  assert.match(await blocked.stderr.readText(), /Error/);
} finally {
  await entry.terminate();
}

// timeout deletes the sandbox
const short = await modal.sandboxes.create(app, undefined, { timeoutMs: 5_000 });
const deadline = Date.now() + 120_000;
while (await exists(short.sandboxId)) {
  assert.ok(Date.now() < deadline, "sandbox outlived its timeout");
  await sleep(5_000);
}

// a CIDR allowlist denies everything else
const allowlisted = await modal.sandboxes.create(app, undefined, { timeoutMs: 120_000, cidrAllowlist: ["1.1.1.1/32"] });
try {
  const run = async (cmd: string) => (await allowlisted.exec(["bash", "-c", cmd])).stdout.readText();
  assert.deepEqual(await reachable(run, "https://1.1.1.1", "https://example.com"), {
    "https://1.1.1.1": true,
    "https://example.com": false,
  });
} finally {
  await allowlisted.terminate();
}

// build steps take env and secrets of their own
const built = await modal.sandboxes.create(
  app,
  modal.images.fromRegistry("python:3.12-slim").dockerfileCommands(["RUN echo $STEP_VAR $BUILD_SECRET > /opt/step.txt"], {
    env: { STEP_VAR: "step" },
    secrets: [await modal.secrets.fromObject({ BUILD_SECRET: "s" })],
    forceBuild: true,
  }),
  { timeoutMs: 120_000 },
);
try {
  assert.equal(await (await built.exec(["bash", "-c", "cat /opt/step.txt; echo $STEP_VAR$BUILD_SECRET"])).stdout.readText(), "step s\n\n");
} finally {
  await built.terminate();
}
assert.throws(() => modal.images.fromRegistry("python:3.12-slim").dockerfileCommands(["RUN true"], { gpu: "A10G" }), NotSupportedError);

// volumes
const volumeName = `e2e-${crypto.randomUUID().slice(0, 12)}`;
await assert.rejects(modal.volumes.fromName(volumeName), NotFoundError);
const volume = await modal.volumes.fromName(volumeName, { createIfMissing: true });
try {
  const writer = await modal.sandboxes.create(app, undefined, { volumes: { "/mnt/data": volume }, timeoutMs: 120_000 });
  const reader = await modal.sandboxes.create(app, undefined, { volumes: { "/data": await modal.volumes.fromName(volumeName) }, timeoutMs: 120_000 });
  try {
    const note = await writer.open("/mnt/data/note.txt", "w");
    await note.write("shared");
    await note.close();
    // A sandbox already running sees the write once it reloads its volumes, as on Modal.
    await reader.reloadVolumes();
    assert.equal(decoder.decode(await (await reader.open("/data/note.txt")).read()), "shared");
  } finally {
    await writer.terminate();
    await reader.terminate();
  }
} finally {
  await modal.volumes.delete(volumeName);
}
await assert.rejects(modal.volumes.delete(volumeName), NotFoundError);
await modal.volumes.delete(volumeName, { allowMissing: true });

console.log("PASS modal live");
