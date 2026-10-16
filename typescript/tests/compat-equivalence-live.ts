import assert from "node:assert/strict";

import { blaxel as createBlaxelProvider } from "@computesdk/blaxel";
import { daytona as createDaytonaProvider } from "@computesdk/daytona";
import { e2b as createE2BProvider } from "@computesdk/e2b";
import { modal as createModalProvider } from "@computesdk/modal";
import { compute, type InstanceInterface as Instance } from "computesdk";

import { SandboxInstance as BlaxelSandbox } from "../src/blaxel.js";
import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";

type Result = { stdout: string; stderr: string; exitCode: number };
type Box = {
  id: string;
  run(command: string, options?: { cwd?: string; env?: Record<string, string>; timeout?: number }): Promise<Result>;
  read(path: string): Promise<string>;
  write(path: string, content: string): Promise<void>;
  reconnect(): Promise<Box>;
  destroy(): Promise<void>;
};

const required = ["ARKER_API_KEY", "ARKER_SOURCE", "E2B_API_KEY", "DAYTONA_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "BL_API_KEY", "BL_WORKSPACE"];
const missing = required.filter((name) => !process.env[name]);
if (missing.length > 0) throw new Error(`Missing live credentials/config: ${missing.join(", ")}`);

function common(instance: Instance, reconnect: () => Promise<Instance>): Box {
  return {
    id: instance.instanceId,
    run: (command, options) => instance.runCommand(command, options),
    read: (path) => instance.filesystem.readFile(path),
    write: (path, content) => instance.filesystem.writeFile(path, content),
    reconnect: async () => common(await reconnect(), reconnect),
    destroy: () => instance.destroy(),
  };
}

async function nativeBox(provider: ReturnType<typeof createE2BProvider> | ReturnType<typeof createDaytonaProvider> | ReturnType<typeof createModalProvider> | ReturnType<typeof createBlaxelProvider>): Promise<Box> {
  const sdk = compute({ provider });
  const instance = await sdk.instances.create({ timeout: 300_000 });
  return common(instance, async () => {
    const found = await sdk.instances.getById(instance.instanceId);
    if (!found) throw new Error(`Native ${provider.name} sandbox ${instance.instanceId} disappeared`);
    return found;
  });
}

async function arkerE2B(): Promise<Box> {
  const sandbox = await E2BSandbox.create();
  const wrap = (value: E2BSandbox): Box => ({
    id: value.sandboxId,
    run: (command, options) => value.commands.run(command, options),
    read: (path) => value.files.read(path),
    write: (path, content) => value.files.write(path, content),
    reconnect: async () => wrap(await E2BSandbox.connect(value.sandboxId)),
    destroy: () => value.kill(),
  });
  return wrap(sandbox);
}

async function arkerDaytona(): Promise<Box> {
  const client = new Daytona({ arker: { apiKey: process.env.ARKER_API_KEY } });
  const sandbox = await client.create();
  const wrap = (value: Awaited<ReturnType<Daytona["create"]>>): Box => ({
    id: value.id,
    run: async (command, options) => {
      const result = await value.process.exec(command, options);
      return { stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode };
    },
    read: async (path) => (await value.process.exec(`cat -- ${JSON.stringify(path)}`)).stdout,
    write: async (path, content) => {
      const encoded = Buffer.from(content).toString("base64");
      const result = await value.process.exec(`printf %s ${JSON.stringify(encoded)} | base64 -d > ${JSON.stringify(path)}`);
      assert.equal(result.exitCode, 0);
    },
    reconnect: async () => wrap(await client.get(value.id)),
    destroy: () => value.delete(),
  });
  return wrap(sandbox);
}

async function arkerModal(): Promise<Box> {
  const client = new ModalClient({ arker: { apiKey: process.env.ARKER_API_KEY } });
  const sandbox = await client.sandboxes.create();
  const wrap = (value: Awaited<ReturnType<typeof client.sandboxes.create>>): Box => ({
    id: value.sandboxId,
    run: async (command, options) => {
      const process = await value.exec(["sh", "-lc", command], {
        workdir: options?.cwd,
        env: options?.env,
        timeoutMs: options?.timeout,
      });
      return { stdout: await process.stdout.readText(), stderr: await process.stderr.readText(), exitCode: await process.wait() };
    },
    read: async (path) => (await wrap(value).run(`cat -- ${JSON.stringify(path)}`)).stdout,
    write: async (path, content) => {
      const encoded = Buffer.from(content).toString("base64");
      const result = await wrap(value).run(`printf %s ${JSON.stringify(encoded)} | base64 -d > ${JSON.stringify(path)}`);
      assert.equal(result.exitCode, 0);
    },
    reconnect: async () => wrap(await client.sandboxes.fromId(value.sandboxId)),
    destroy: () => value.terminate(),
  });
  return wrap(sandbox);
}

async function arkerBlaxel(): Promise<Box> {
  const config = { arker: { apiKey: process.env.ARKER_API_KEY } };
  const sandbox = await BlaxelSandbox.create({}, config);
  const wrap = (value: BlaxelSandbox): Box => ({
    id: value.id,
    run: (command, options) => value.process.exec({ command, workingDir: options?.cwd, env: options?.env, timeout: options?.timeout }),
    read: (path) => value.fs.read(path),
    write: (path, content) => value.fs.write(path, content),
    reconnect: async () => wrap(await BlaxelSandbox.get(value.id, config)),
    destroy: () => value.delete(),
  });
  return wrap(sandbox);
}

type Observation = {
  defaultDirectory: string;
  cwd: Result;
  env: Result;
  failure: Result;
  persistence: string;
  reconnectPersistence: string;
  concurrent: string[];
};

async function observe(box: Box): Promise<Observation> {
  const marker = `/tmp/computesdk-equivalence-${Date.now()}-${Math.random().toString(16).slice(2)}.txt`;
  const defaultDirectory = (await box.run("pwd")).stdout.trim();
  const cwd = await box.run("pwd", { cwd: "/tmp" });
  const env = await box.run("printf %s \"$COMPUTESDK_EQ\"", { env: { COMPUTESDK_EQ: "a value with spaces" } });
  const failure = await box.run("printf out; printf err >&2; exit 17");
  await box.write(marker, "first");
  assert.equal(await box.read(marker), "first");
  await box.run(`printf second >> ${JSON.stringify(marker)}`);
  const persistence = await box.read(marker);
  const reconnected = await box.reconnect();
  const reconnectPersistence = await reconnected.read(marker);
  const concurrent = await Promise.all([
    box.run("sleep 0.2; printf one"),
    box.run("sleep 0.1; printf two"),
  ]).then((values) => values.map((value) => value.stdout).sort());
  return { defaultDirectory, cwd, env, failure, persistence, reconnectPersistence, concurrent };
}

function assertSemantics(provider: string, observation: Observation): void {
  assert.equal(observation.cwd.stdout.trim(), "/tmp", `${provider}: cwd`);
  assert.equal(observation.cwd.exitCode, 0, `${provider}: cwd exit`);
  assert.equal(observation.env.stdout, "a value with spaces", `${provider}: env`);
  assert.equal(observation.failure.stdout, "out", `${provider}: stdout`);
  assert.equal(observation.failure.stderr, "err", `${provider}: stderr`);
  assert.equal(observation.failure.exitCode, 17, `${provider}: exit code`);
  assert.equal(observation.persistence, "firstsecond", `${provider}: multi-turn persistence`);
  assert.equal(observation.reconnectPersistence, "firstsecond", `${provider}: reconnect persistence`);
  assert.deepEqual(observation.concurrent, ["one", "two"], `${provider}: concurrent execution`);
}

const pairs = [
  ["e2b", () => nativeBox(createE2BProvider({ apiKey: process.env.E2B_API_KEY })), arkerE2B],
  ["daytona", () => nativeBox(createDaytonaProvider({ apiKey: process.env.DAYTONA_API_KEY })), arkerDaytona],
  ["modal", () => nativeBox(createModalProvider({ tokenId: process.env.MODAL_TOKEN_ID, tokenSecret: process.env.MODAL_TOKEN_SECRET })), arkerModal],
  ["blaxel", () => nativeBox(createBlaxelProvider({ apiKey: process.env.BL_API_KEY, workspace: process.env.BL_WORKSPACE })), arkerBlaxel],
] as const;

for (const [provider, createNative, createArker] of pairs) {
  console.log(`START ${provider}`);
  let native: Box | undefined;
  let arker: Box | undefined;
  try {
    native = await createNative();
    arker = await createArker();
    const nativeObservation = await observe(native);
    const arkerObservation = await observe(arker);
    assertSemantics(`${provider}/native`, nativeObservation);
    assertSemantics(`${provider}/arker`, arkerObservation);
    assert.equal(arkerObservation.defaultDirectory, nativeObservation.defaultDirectory, `${provider}: default directory parity`);
    console.log(`PASS ${provider}`, {
      nativeDefaultDirectory: nativeObservation.defaultDirectory,
      arkerDefaultDirectory: arkerObservation.defaultDirectory,
    });
  } finally {
    await Promise.allSettled([native?.destroy(), arker?.destroy()]);
  }
}

console.log("PASS compatibility equivalence matrix");
process.exit(0);
