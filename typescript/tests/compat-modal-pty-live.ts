import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModalClient as Native } from "modal";
import { ModalClient as Shim } from "../src/modal.js";
import { ModalCleanup, recordEvidence } from "../scripts/compat-evidence.js";

interface CoreClient<A, I> {
  apps: { fromName(name: string, options: { createIfMissing: true }): Promise<A> };
  images: { fromRegistry(tag: string): I };
  sandboxes: { create(app: A, image: I, options: { timeoutMs: number }): Promise<{
    sandboxId: string;
    exec(command: string[], options: { pty: true; env: Record<string, string> }): Promise<import("modal").ContainerProcess<string>>;
    terminate(): Promise<void>;
    poll(): Promise<number | null>;
  }>; fromId(id: string): Promise<{ sandboxId: string; poll(): Promise<number | null> }> };
}

async function workload<A, I>(client: CoreClient<A, I>, called: Set<string>, cleanup: ModalCleanup) {
  called.add("ModalClient.apps.fromName");
  const app = await client.apps.fromName("arker-compat-lifecycle", { createIfMissing: true });
  called.add("ModalClient.images.fromRegistry");
  const image = client.images.fromRegistry("python:3.12-slim-bookworm");
  called.add("ModalClient.sandboxes.create");
  const box = await client.sandboxes.create(app, image, { timeoutMs: 60_000 });
  cleanup.allocated(box.sandboxId);
  console.log(JSON.stringify({ event: "allocated", id: box.sandboxId }));
  try {
    const code = 'import os,json,sys;line=sys.stdin.readline();print("RESULT:"+json.dumps({"tty":[os.isatty(n) for n in range(3)],"size":list(os.get_terminal_size()),"term":os.getenv("TERM"),"color":os.getenv("COLORTERM"),"line":line}));os.write(2,b"stderr-marker\\n");raise SystemExit(7)';
    called.add("Sandbox.exec");
    const process = await box.exec(["python3", "-c", code], { pty: true, env: { TERM: "explicit-term" } });
    called.add("ContainerProcess.stdin.getWriter");
    const writer = process.stdin.getWriter();
    try { await writer.write("input\n"); }
    finally { writer.releaseLock(); }
    called.add("ContainerProcess.stdout.readText");
    const output = await process.stdout.readText();
    called.add("ContainerProcess.stderr.readText");
    assert.equal(await process.stderr.readText(), "");
    called.add("ContainerProcess.wait");
    assert.equal(await process.wait(), 7);
    const record = output.split("\r\n").find(line => line.startsWith("RESULT:"));
    assert.ok(record, output);
    assert.deepEqual(JSON.parse(record.slice(7)), { tty: [true, true, true], size: [80, 24], term: "explicit-term", color: "truecolor", line: "input\n" });
    assert.ok(output.includes("stderr-marker\r\n"), output);
    return box.sandboxId;
  } finally {
    called.add("Sandbox.terminate");
    await box.terminate();
    const until = performance.now() + 75_000;
    called.add("Sandbox.poll");
    while (await box.poll() === null) {
      assert.ok(performance.now() < until, "Owned sandbox cleanup unconfirmed");
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    await cleanup.observe(box.sandboxId, id => client.sandboxes.fromId(id));
    console.log(JSON.stringify({ event: "cleanup_confirmed", id: box.sandboxId }));
  }
}

const route = process.env.MODAL_PTY_ROUTE ?? "native";
if (!["native", "modal", "arker"].includes(route)) throw new Error("Unknown Modal PTY route");
if (route === "arker") {
  if (!process.env.ARKER_API_KEY) throw new Error("Arker credentials required");
  delete process.env.MODAL_TOKEN_ID;
  delete process.env.MODAL_TOKEN_SECRET;
} else if (!process.env.MODAL_TOKEN_ID || !process.env.MODAL_TOKEN_SECRET) throw new Error("Modal credentials required");
const directory = await mkdtemp(join(tmpdir(), "modal-pty-"));
const config = join(directory, "router.json");
await writeFile(config, JSON.stringify({ version: 1,
  existing: { strategy: "priority", order: [route === "arker" ? "arker" : "modal"] },
  create: { strategy: "priority", order: [route === "arker" ? "arker" : "modal"] },
}));
const called = new Set<string>();
const cleanup = new ModalCleanup();
let outcome = "failed";
try {
  const client = route === "native" ? new Native() : new Shim({ routerConfigPath: config });
  try {
    console.log(JSON.stringify({ route, id: await workload(client, called, cleanup), passed: true, cleanupRequested: true }));
  } finally { client.close(); }
  outcome = "passed";
} finally {
  try {
    await recordEvidence(
      "modal",
      route === "native" ? "reference" : route === "modal" ? "native" : "arker",
      "pty-text-terminal-input-output",
      [...called].sort(),
      outcome, {}, cleanup.receipt(),
    );
  } finally { await rm(directory, { recursive: true }); }
}
