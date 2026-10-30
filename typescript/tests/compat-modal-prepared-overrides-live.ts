import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
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
  terminate(options?: { wait: true }): Promise<unknown>;
}
interface CoreClient<A, I, S> {
  apps: { fromName(name: string, options: { createIfMissing: true }): Promise<A> };
  images: { fromRegistry(tag: string): { dockerfileCommands(commands: string[]): I } };
  secrets: { fromObject(values: Record<string, string>): Promise<S> };
  sandboxes: {
    create(app: A, image: I, options: { secrets: S[]; env: Record<string, string>; workdir: string; timeoutMs: number }): Promise<CoreSandbox>;
    fromId(id: string): Promise<CoreSandbox>;
  };
}

// Every route executes this identical workload, including creation arguments.
async function workload<A, I, S>(client: CoreClient<A, I, S>) {
  const app = await client.apps.fromName("arker-compat-recipes", { createIfMissing: true });
  const slow = process.env.MODAL_PREPARED_SLOW === "1";
  const delay = slow ? 35 : 0;
  const startup = "import os,time; assert os.getcwd() == '/tmp'; " +
    "assert os.environ['PREPARED_SECRET'] == 'synthetic-only'; " +
    "assert os.environ['PRECEDENCE'] == 'creation'; " +
    "open('/tmp/prepared-started','w').write('startup-ok'); time.sleep(300)";
  const image = client.images.fromRegistry("python:3.12-slim-bookworm").dockerfileCommands([
    `RUN sleep ${delay} && touch /tmp/build-${randomUUID()}`,
    "ENTRYPOINT []", "CMD " + JSON.stringify(["python", "-c", startup]),
  ]);
  const secret = await client.secrets.fromObject({ PREPARED_SECRET: "synthetic-only", PRECEDENCE: "secret" });
  const started = performance.now();
  const box = await client.sandboxes.create(app, image, {
    secrets: [secret], env: { PRECEDENCE: "creation" }, workdir: "/tmp", timeoutMs: 30_000,
  });
  console.log(JSON.stringify({ event: "allocated", id: box.sandboxId }));
  try {
    if (slow) assert(performance.now() - started >= delay * 1000);
    const connected = await client.sandboxes.fromId(box.sandboxId);
    const check = "import os,pathlib,time; p=pathlib.Path('/tmp/prepared-started'); deadline=time.monotonic()+10\n" +
      "while not p.exists() and time.monotonic()<deadline: time.sleep(.1)\n" +
      "assert p.read_text() == 'startup-ok'; assert os.getcwd() == '/tmp'; " +
      "assert os.environ['PREPARED_SECRET'] == 'synthetic-only'; " +
      "assert os.environ['PRECEDENCE'] == 'exec'; time.sleep(5); print('prepared-ok')";
    const child = await connected.exec(["python", "-c", check], { env: { PRECEDENCE: "exec" } });
    assert.equal(await child.stdout.readText(), "prepared-ok\n");
    assert.equal(await child.wait(), 0);
    return box.sandboxId;
  } finally {
    await box.terminate();
    console.log(JSON.stringify({ event: "cleanup_requested", id: box.sandboxId }));
  }
}

const route = process.env.MODAL_PREPARED_ROUTE ?? "native";
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
    try { console.log(JSON.stringify({ route, id: await workload(client), passed: true, cleanupRequested: true })); }
    finally { client.close(); }
  } else {
    const client = new Shim({ routerConfigPath: config });
    try { console.log(JSON.stringify({ route, id: await workload(client), passed: true, cleanupRequested: true })); }
    finally { client.close(); }
  }
} finally { await rm(directory, { recursive: true }); }
