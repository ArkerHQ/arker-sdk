import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModalClient as Native } from "modal";
import { ModalClient as Shim } from "../src/modal.js";

interface CoreProcess {
  stdout: { readText(): Promise<string> };
  wait(): Promise<number>;
}
interface CoreSandbox {
  sandboxId: string;
  exec(command: string[], options: { env: Record<string, string> }): Promise<CoreProcess>;
  terminate(): Promise<unknown>;
}
interface CoreClient<A, I, S> {
  apps: { fromName(name: string, options: { createIfMissing: true }): Promise<A> };
  images: { fromRegistry(tag: string): I };
  secrets: { fromObject(values: Record<string, string>): Promise<S> };
  sandboxes: {
    create(app: A, image: I, options: { secrets: S[]; env: Record<string, string>; workdir: string; timeoutMs: number }): Promise<CoreSandbox>;
    fromId(id: string): Promise<CoreSandbox>;
  };
}

// Every route executes this identical workload, including creation arguments.
async function workload<A, I, S>(client: CoreClient<A, I, S>) {
  const app = await client.apps.fromName("arker-compat-recipes", { createIfMissing: true });
  const image = client.images.fromRegistry("python:3.12-slim-bookworm");
  const secret = await client.secrets.fromObject({ RECIPE_VALUE: "synthetic-fixture", PRECEDENCE: "secret" });
  const box = await client.sandboxes.create(app, image, {
    secrets: [secret], env: { PRECEDENCE: "creation" }, workdir: "/tmp", timeoutMs: 180_000,
  });
  try {
    const connected = await client.sandboxes.fromId(box.sandboxId);
    const process = await connected.exec(["python", "-c",
      "import os,sys; assert sys.version_info[:2] == (3,12); assert os.getcwd() == '/tmp'; " +
      "assert os.environ['RECIPE_VALUE'] == 'synthetic-fixture'; assert os.environ['PRECEDENCE'] == 'exec'; print('recipe-ok')"],
    { env: { PRECEDENCE: "exec" } });
    assert.equal(await process.stdout.readText(), "recipe-ok\n");
    assert.equal(await process.wait(), 0);
    return box.sandboxId;
  } finally { await box.terminate(); }
}

const route = process.env.MODAL_RECIPE_ROUTE ?? "native";
if (!["native", "modal", "arker"].includes(route)) throw new Error("Unknown Modal recipe route");
if (route === "arker") {
  if (!process.env.ARKER_API_KEY) throw new Error("Arker credentials required");
  delete process.env.MODAL_TOKEN_ID;
  delete process.env.MODAL_TOKEN_SECRET;
} else if (!process.env.MODAL_TOKEN_ID || !process.env.MODAL_TOKEN_SECRET) throw new Error("Modal credentials required");
const directory = await mkdtemp(join(tmpdir(), "modal-recipes-"));
const config = join(directory, "router.json");
await writeFile(config, JSON.stringify({ version: 1,
  existing: { strategy: "priority", order: [route === "arker" ? "arker" : "modal"] },
  create: { strategy: "priority", order: [route === "arker" ? "arker" : "modal"] },
}));
try {
  if (route === "native") {
    const client = new Native();
    try { console.log(JSON.stringify({ route, id: await workload(client), passed: true, cleaned: true })); }
    finally { client.close(); }
  } else {
    const client = new Shim({ routerConfigPath: config });
    try { console.log(JSON.stringify({ route, id: await workload(client), passed: true, cleaned: true })); }
    finally { client.close(); }
  }
} finally { await rm(directory, { recursive: true }); }
