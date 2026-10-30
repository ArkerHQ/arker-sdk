import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModalClient as Native } from "modal";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { ClientError, Status } = createRequire(require.resolve("modal"))("nice-grpc");
import { ModalClient as Shim } from "../src/modal.js";

interface CoreSandbox {
  sandboxId: string;
  poll(): Promise<number | null>;
  wait(): Promise<number>;
  exec(command: string[]): Promise<unknown>;
  terminate(params?: { wait: true }): Promise<unknown>;
}
interface CoreClient<A, I> {
  apps: { fromName(name: string, options: { createIfMissing: true }): Promise<A> };
  images: { fromRegistry(tag: string): I };
  sandboxes: {
    create(app: A, image: I, options: { timeoutMs: number }): Promise<CoreSandbox>;
    fromId(id: string): Promise<CoreSandbox>;
  };
}

// Identical creation arguments and lifecycle calls on all three routes.
async function workload<A, I>(client: CoreClient<A, I>) {
  const app = await client.apps.fromName("arker-compat-lifecycle", { createIfMissing: true });
  const image = client.images.fromRegistry("python:3.12-slim-bookworm");
  const reason = process.env.MODAL_TERMINAL_REASON ?? "requested";
  if (!["requested", "deadline"].includes(reason)) throw new Error("Unknown terminal scenario");
  const box = await client.sandboxes.create(app, image, { timeoutMs: 30_000 });
  console.log(JSON.stringify({ event: "allocated", id: box.sandboxId }));
  try {
    assert.equal(await box.poll(), null);
    if (reason === "requested") assert.equal(await box.terminate({ wait: true }), 137);
    else {
      const deadline = performance.now() + 120_000;
      while (await box.poll() === null) {
        assert(performance.now() < deadline, "Sandbox deadline did not terminate execution");
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    }
    const found = await client.sandboxes.fromId(box.sandboxId);
    assert.equal(found.sandboxId, box.sandboxId);
    assert.equal(await found.poll(), reason === "deadline" ? 124 : 137);
    assert.equal(await found.wait(), reason === "deadline" ? 124 : 137);
    await assert.rejects(found.exec(["true"]), (error: unknown) => {
      assert.ok(error instanceof ClientError);
      assert.equal(Object.getPrototypeOf(error), ClientError.prototype);
      assert.equal((error as { code: number }).code, Status.NOT_FOUND);
      assert.equal((error as { path: string }).path, "/modal.task_command_router.TaskCommandRouter/TaskExecStart");
      return true;
    });
    return box.sandboxId;
  } finally {
    await box.terminate();
    console.log(JSON.stringify({ event: "cleanup_requested", id: box.sandboxId }));
  }
}

const route = process.env.MODAL_TERMINAL_ROUTE ?? "native";
if (!["native", "modal", "arker"].includes(route)) throw new Error("Unknown Modal terminal route");
if (route === "arker") {
  if (!process.env.ARKER_API_KEY) throw new Error("Arker credentials required");
  delete process.env.MODAL_TOKEN_ID;
  delete process.env.MODAL_TOKEN_SECRET;
} else if (!process.env.MODAL_TOKEN_ID || !process.env.MODAL_TOKEN_SECRET) throw new Error("Modal credentials required");
const directory = await mkdtemp(join(tmpdir(), "modal-terminal-"));
const config = join(directory, "router.json");
await writeFile(config, JSON.stringify({ version: 1,
  existing: { strategy: "priority", order: [route === "arker" ? "arker" : "modal"] },
  create: { strategy: "priority", order: [route === "arker" ? "arker" : "modal"] },
}));
try {
  if (route === "native") {
    const client = new Native();
    try { console.log(JSON.stringify({ route, id: await workload(client), passed: true, cleanupRequested: true })); }
    finally { client.close(); }
  } else {
    const client = new Shim({ routerConfigPath: config });
    try { console.log(JSON.stringify({ route, id: await workload(client), passed: true, cleanupRequested: true })); }
    finally { client.close(); }
  }
} finally { await rm(directory, { recursive: true }); }
