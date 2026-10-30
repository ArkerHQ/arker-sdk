import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import * as Native from "e2b";
import * as Shim from "../src/e2b.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";
import { traceVendor } from "./compat-trace.js";

const cases = ["watch-directory", "watch-recursive", "home-reconnect-user", "stream-read", "stream-cancel", "file-read-errors", "stream-abort", "file-overwrite", "file-batch-rename", "public-app", "private-app", "pty", "pty-reconnect", "pty-kill", "pause-full", "pause-filesystem", "volume-mount"] as const;
type Case = (typeof cases)[number];
const selectedCases = process.env.COMPAT_E2B_CASES?.split(",") ?? [...cases];
for (const name of selectedCases) assert.ok(cases.includes(name as Case), `Unknown E2B scenario: ${name}`);

// The program uses the vendor API unchanged; only its imported module differs.
async function program({ Sandbox, Volume, CommandExitError, SandboxError }: typeof Native, scenario: Case): Promise<void> {
  if (scenario === "volume-mount") {
    const volume = await Volume.create(`compat-core-${randomUUID()}`);
    try {
      for (const writing of [true, false]) {
        const sandbox = await Sandbox.create({ volumeMounts: { "/mnt/compat": volume } });
        try {
          if (writing) await sandbox.files.write("/mnt/compat/persist", "volume-data", { user: "root" });
          assert.equal(await sandbox.files.read("/mnt/compat/persist", { user: "root" }), "volume-data");
        } finally {
          await sandbox.kill();
        }
      }
    } finally {
      assert.equal(await Volume.destroy(volume.volumeId), true);
    }
    return;
  }
  const sandbox = await Sandbox.create(scenario === "private-app" ? { network: { allowPublicTraffic: false } } : {});
  try {
    if (scenario === "file-read-errors") {
      const path = `/tmp/missing-${randomUUID()}`;
      const reads = [
        () => sandbox.files.read(path, { format: "text" }),
        () => sandbox.files.read(path, { format: "bytes" }),
        () => sandbox.files.read(path, { format: "blob" }),
        () => sandbox.files.read(path, { format: "stream" }),
      ];
      for (const read of reads) {
        await assert.rejects(read, (error: unknown) => {
          assert.ok(error instanceof Native.FileNotFoundError);
          assert.equal(error.constructor, Native.FileNotFoundError);
          assert.equal(error.statusCode, undefined);
          return true;
        });
        assert.equal(await sandbox.files.exists(path), false);
      }
      await sandbox.files.write(path, "readable");
      assert.equal(await sandbox.files.read(path), "readable");
    } else if (scenario === "stream-abort") {
      const path = `/tmp/stream-abort-${randomUUID()}`;
      const payload = Uint8Array.from({ length: 8 * 1024 * 1024 }, (_, index) => index % 251);
      await sandbox.files.write(path, payload.buffer);
      const controller = new AbortController();
      const reader = (await sandbox.files.read(path, { format: "stream", signal: controller.signal })).getReader();
      try {
        const first = await reader.read();
        assert.equal(first.done, false);
        assert.ok(first.value && first.value.length > 0 && first.value.length < payload.length);
        assert.deepEqual(first.value, payload.slice(0, first.value.length));
        controller.abort();
        await assert.rejects(async () => {
          // Drain queued chunks so the assertion observes the stream's terminal state.
          while (!(await reader.read()).done) {}
        }, (error: unknown) => {
          assert.ok(error instanceof DOMException);
          assert.equal(error.constructor, DOMException);
          assert.equal(error.name, "AbortError");
          assert.equal(error, controller.signal.reason);
          return true;
        });
      } finally {
        await reader.cancel().catch(error => { if (error !== controller.signal.reason) throw error; });
        reader.releaseLock();
      }
      assert.equal(controller.signal.aborted, true);
      assert.deepEqual(await sandbox.files.read(path, { format: "bytes" }), payload);
      assert.equal((await sandbox.commands.run("printf alive")).stdout, "alive");
    } else if (scenario === "watch-directory" || scenario === "watch-recursive") {
      const root = `/home/user/watch-${randomUUID()}`;
      const recursive = scenario === "watch-recursive";
      const name = recursive ? "nested/event.txt" : "event.txt";
      await sandbox.files.makeDir(`${root}/nested`);
      const events: Native.FilesystemEvent[] = [];
      const exits: (Error | undefined)[] = [];
      let exit!: () => void;
      const exited = new Promise<void>(resolve => { exit = resolve; });
      const watcher = await sandbox.files.watchDir(root, event => { events.push(event); }, {
        recursive, includeEntry: true, timeoutMs: 0,
        onExit: error => { exits.push(error); exit(); },
      });
      const until = async (check: () => boolean) => {
        const deadline = Date.now() + 30000;
        while (!check()) {
          assert.ok(Date.now() < deadline, "Watch event was not delivered");
          await new Promise(resolve => setTimeout(resolve, 10));
        }
      };
      try {
        await sandbox.files.write(`${root}/${name}`, "watch-data");
        await until(() => events.some(event => event.name === name && event.type === "create"));
        const created = events.find(event => event.name === name && event.type === "create")!;
        assert.equal(created.entry?.path, `${root}/${name}`);
        assert.equal(created.entry?.name, "event.txt");
        await sandbox.files.remove(`${root}/${name}`);
        await until(() => events.some(event => event.name === name && event.type === "remove"));
      } finally {
        await watcher.stop();
      }
      await until(() => exits.length > 0);
      await exited;
      assert.deepEqual(exits, [undefined]);
      await watcher.stop();
      assert.deepEqual(exits, [undefined]);
    } else if (scenario === "public-app" || scenario === "private-app") {
      await sandbox.commands.run("python3 -m http.server 8080 --bind 0.0.0.0", { background: true, timeoutMs: 0 });
      await sandbox.commands.run(`python3 - <<'PY'
import time, urllib.request
for attempt in range(100):
    try:
        urllib.request.urlopen('http://127.0.0.1:8080', timeout=1).close()
        break
    except OSError:
        time.sleep(.1)
else:
    raise RuntimeError('application did not start')
PY`, { timeoutMs: 15000 });
      const response = await fetch(`https://${sandbox.getHost(8080)}`, { redirect: "error", signal: AbortSignal.timeout(15000) });
      try { assert.equal(response.ok, scenario === "public-app"); }
      finally { await response.body?.cancel(); }
    } else if (scenario === "file-batch-rename") {
      const directory = `/home/user/compat-batch-${randomUUID()}`;
      await sandbox.files.makeDir(directory);
      const paths = [`${directory}/first.txt`, `${directory}/second.bin`];
      const written = await sandbox.files.writeFiles([
        { path: paths[0], data: "hello" }, { path: paths[1], data: Uint8Array.of(0, 128, 255).buffer },
      ]);
      assert.deepEqual(written.map(file => file.path), paths);
      assert.equal((await sandbox.files.getInfo(paths[0])).size, 5);
      const renamed = `${directory}/renamed.txt`;
      assert.equal((await sandbox.files.rename(paths[0], renamed)).path, renamed);
      assert.equal(await sandbox.files.exists(paths[0]), false);
      assert.equal(await sandbox.files.read(renamed), "hello");
      assert.deepEqual(await sandbox.files.read(paths[1], { format: "bytes" }), Uint8Array.of(0, 128, 255));
    } else if (scenario === "home-reconnect-user") {
      await sandbox.files.write("compat-home", "shared-home", { user: "user" });
      assert.equal((await sandbox.commands.run("cat ~/compat-home")).stdout, "shared-home");
      const connected = await Sandbox.connect(sandbox.sandboxId);
      assert.equal(connected.sandboxId, sandbox.sandboxId);
      assert.equal(await connected.files.read("/home/user/compat-home", { user: "user" }), "shared-home");
      await connected.commands.run("printf changed > ~/compat-home");
      assert.equal(await sandbox.files.read("compat-home", { user: "user" }), "changed");
      await sandbox.files.write("relative-root", "root-home", { user: "root" });
      assert.equal(await sandbox.files.read("~/relative-root", { user: "root" }), "root-home");
      assert.equal(await sandbox.files.read("/root/relative-root"), "root-home");
      assert.equal(await sandbox.files.read("~/compat-home"), "changed");
      await sandbox.commands.run("printf secret > /root/compat-secret; chmod 600 /root/compat-secret", { user: "root" });
      assert.equal(await connected.files.read("/root/compat-secret", { user: "root" }), "secret");
      assert.equal(await connected.files.read("/root/compat-secret", { user: "user" }), "secret");
      assert.equal((await sandbox.commands.run("test ! -r /root/compat-secret", { user: "user" })).exitCode, 0);
      await sandbox.commands.run("chmod 000 /root/compat-secret", { user: "root" });
      await sandbox.files.write("/root/compat-secret", "updated", { user: "user" });
      assert.equal(await sandbox.files.read("/root/compat-secret", { user: "user" }), "updated");
      const info = await sandbox.files.getInfo("/root/compat-secret", { user: "user" });
      assert.deepEqual([info.owner, info.group, info.mode & 0o777], ["user", "user", 0]);
      assert.equal(await sandbox.files.exists("/root/compat-secret", { user: "user" }), true);
      assert.ok((await sandbox.files.list("/root", { user: "user" })).some(entry => entry.name === "compat-secret"));
      assert.equal(await sandbox.files.makeDir("/root/compat-user-dir/nested", { user: "user" }), true);
      const directory = await sandbox.files.getInfo("/root/compat-user-dir/nested", { user: "user" });
      assert.deepEqual([directory.owner, directory.group, directory.mode & 0o777], ["user", "user", 0o755]);
      await sandbox.files.remove("/root/compat-secret", { user: "user" });
      assert.equal(await sandbox.files.exists("/root/compat-secret", { user: "user" }), false);
      await sandbox.files.remove("/root/compat-user-dir", { user: "user" });
    } else if (scenario === "file-overwrite") {
      const first = Uint8Array.from({ length: 6 * 1024 * 1024 }, (_, i) => i % 256);
      const temporary = `/tmp/compat-overwrite-${randomUUID()}`;
      await sandbox.files.write(temporary, first.buffer, { user: "user" });
      assert.equal((await sandbox.commands.run("cat /proc/sys/fs/protected_regular")).stdout.trim(), "2");
      await assert.rejects(sandbox.files.write(temporary, "replacement", { user: "user" }),
        error => error instanceof SandboxError && /error opening file:.*permission denied/.test(error.message));
      assert.deepEqual(await sandbox.files.read(temporary, { format: "bytes" }), first);
      for (const payload of [first, new TextEncoder().encode("short"), first]) {
        await sandbox.files.write("compat-overwrite", payload.buffer, { user: "user" });
        assert.deepEqual(await sandbox.files.read("compat-overwrite", { format: "bytes" }), payload);
        const reader = (await sandbox.files.read("compat-overwrite", { format: "stream" })).getReader();
        try {
          let offset = 0;
          for (;;) {
            const chunk = await reader.read();
            if (chunk.done) break;
            assert.deepEqual(chunk.value, payload.slice(offset, offset + chunk.value.length));
            offset += chunk.value.length;
          }
          assert.equal(offset, payload.length);
        } finally { await reader.cancel(); reader.releaseLock(); }
        assert.equal((await sandbox.files.getInfo("compat-overwrite")).size, payload.length);
      }
    } else if (scenario === "stream-read" || scenario === "stream-cancel") {
      const payload = Uint8Array.from({ length: 4 * 1024 * 1024 }, (_, i) => i % 256);
      await sandbox.files.write("/tmp/compat-stream", payload.buffer);
      const stream = await sandbox.files.read("/tmp/compat-stream", { format: "stream" });
      const reader = stream.getReader();
      try {
        const first = await reader.read();
        assert.equal(first.done, false);
        assert.ok(first.value && first.value.length > 0 && first.value.length < payload.length);
        assert.deepEqual(first.value, payload.slice(0, first.value.length));
        if (scenario === "stream-read") {
          let offset = first.value.length;
          for (;;) {
            const chunk = await reader.read();
            if (chunk.done) break;
            assert.deepEqual(chunk.value, payload.slice(offset, offset + chunk.value.length));
            offset += chunk.value.length;
          }
          assert.equal(offset, payload.length);
        }
      } finally {
        await reader.cancel();
        reader.releaseLock();
      }
      assert.equal((await sandbox.commands.run("printf alive")).stdout, "alive");
      assert.deepEqual(await sandbox.files.read("/tmp/compat-stream", { format: "bytes" }), payload);
    } else if (scenario === "pty" || scenario === "pty-reconnect" || scenario === "pty-kill") {
      const output: Uint8Array[] = [];
      const onData = (data: Uint8Array) => { output.push(data); };
      let terminal = await sandbox.pty.create({ rows: 24, cols: 80, timeoutMs: 0, onData });
      const pid = terminal.pid;
      assert.ok(Number.isSafeInteger(pid) && pid > 0);
      if (scenario === "pty") {
        await sandbox.pty.resize(pid, { rows: 37, cols: 91 });
        await sandbox.pty.sendInput(pid, new TextEncoder().encode("stty size > /tmp/compat-pty-size; printf '\\120\\124\\131_OK'; exit\n"));
        assert.equal((await terminal.wait()).exitCode, 0);
        assert.match(Buffer.concat(output).toString(), /PTY_OK/);
        assert.equal((await sandbox.files.read("/tmp/compat-pty-size")).trim(), "37 91");
      } else {
        if (scenario === "pty-reconnect") {
          await terminal.disconnect();
          output.length = 0;
          terminal = await sandbox.pty.connect(pid, { timeoutMs: 0, onData });
          assert.equal(terminal.pid, pid);
          await sandbox.pty.sendInput(pid, new TextEncoder().encode("printf '\\120\\124\\131_RECONNECTED\\377\\000'; exit 7\n"));
        } else {
          assert.equal(await sandbox.pty.kill(pid), true);
        }
        await assert.rejects(terminal.wait(), (error: unknown) => {
          assert.ok(error instanceof CommandExitError);
          assert.equal(error.exitCode, scenario === "pty-reconnect" ? 7 : -1);
          assert.equal(error.stdout, "");
          assert.equal(error.stderr, "");
          return true;
        });
        assert.ok(output.every((chunk) => chunk instanceof Uint8Array));
        if (scenario === "pty-reconnect") {
          assert.ok(Buffer.concat(output).includes(Buffer.concat([Buffer.from("PTY_RECONNECTED"), Buffer.from([255, 0])])));
        } else {
          // Native returns false on NOT_FOUND; retention after exit is server-dependent.
          const repeated = await sandbox.pty.kill(pid);
          assert.equal(typeof repeated, "boolean");
          console.log(`PTY kill after wait returned ${repeated}`);
        }
      }
    } else {
      const mode = scenario === "pause-full" ? "full" : "filesystem";
      await sandbox.files.write("compat-persist", "disk-state");
      const process = await sandbox.commands.run("python3 -c 'import signal; signal.pause()' compat-memory-marker", { background: true, timeoutMs: 0 });
      assert.equal(await sandbox.pause({ mode }), true);
      const connected = await Sandbox.connect(sandbox.sandboxId);
      assert.equal(connected.sandboxId, sandbox.sandboxId);
      assert.equal(await connected.files.read("compat-persist"), "disk-state");
      const state = (await connected.commands.run(`if test -r /proc/${process.pid}/cmdline; then cat /proc/${process.pid}/cmdline; fi`)).stdout;
      assert.equal(state.includes("compat-memory-marker"), mode === "full");
    }
  } finally {
    await sandbox.kill();
  }
}

await withLiveRouter(["E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"], async ({ setProvider }) => {
  assert.ok(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || process.env.ARKER_SOURCE_VM_ID, "An isolated source is required");
  const nativeKey = process.env.E2B_API_KEY;
  const failures: Error[] = [];
  for (const [provider, module] of [["e2b", Native], ["e2b", Shim], ["arker", Shim]] as const) {
    await setProvider(provider);
    if (provider === "arker") delete process.env.E2B_API_KEY;
    else process.env.E2B_API_KEY = nativeKey;
    for (const scenario of selectedCases as Case[]) {
      const called = new Set<string>();
      let outcome = "failed";
      try {
        // No shim implementation fallback: absent exports or methods fail this case.
        const vendor = module as unknown as typeof Native;
        await program({
          ...vendor,
          Sandbox: traceVendor(vendor.Sandbox, "Sandbox.static", called),
          ...(vendor.Volume ? { Volume: traceVendor(vendor.Volume, "Volume.static", called) } : {}),
        }, scenario);
        outcome = "passed";
      } catch (error) {
        console.error(`Failed ${scenario} on ${provider}: ${error instanceof Error ? error.name : typeof error}`, error && typeof error === "object" && "status" in error ? { status: error.status, code: "code" in error ? error.code : undefined } : {});
        failures.push(new Error(`import-only-core-${scenario}: ${provider}/${module === Native ? "reference" : "shim"} failed`));
      } finally {
        await recordEvidence("e2b", module === Native ? "reference" : provider === "arker" ? "arker" : "native",
          `import-only-core-${scenario}`, [...called].sort(), outcome);
      }
    }
  }
  if (failures.length) throw new AggregateError(failures, "Extended core scenarios failed; inspect failed evidence rows");
});
