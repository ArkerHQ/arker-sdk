import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { Daytona as Native, DaytonaBadRequestError, DaytonaConflictError, DaytonaNotFoundError } from "@daytona/sdk";

import { Arker } from "../src/index.js";
import { Daytona as Shim } from "../src/daytona.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";

const APIS = [
  "Daytona.volume.create", "Daytona.volume.get", "Daytona.volume.list", "Daytona.create", "Daytona.delete",
  "Sandbox.fs.uploadFile", "Sandbox.fs.downloadFile", "Sandbox.fs.moveFiles", "Sandbox.fs.deleteFile",
];
type Scenario = "shared-volume-filesystem" | "subpath-filesystem" | "get-create-concurrency" | "delete-mounted";

async function eventually(check: () => Promise<void>): Promise<void> {
  const deadline = Date.now() + 30_000;
  for (;;) {
    try { await check(); return; }
    catch (error) {
      if (Date.now() >= deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
}

export async function sharedFilesystemProgram(
  Client: typeof Native | typeof Shim,
  cleanupVolume?: (id: string) => Promise<void>,
  progress: (stage: string) => void = () => {},
  scenario: Scenario = "shared-volume-filesystem",
): Promise<void> {
  const client = new Client();
  const name = `compat-volume-${randomUUID()}`;
  const mountPath = "/mnt/compat-volume";
  let volume: any;
  const sandboxes: any[] = [];
  let primary: unknown;
  const ready = async (value: any) => {
    const deadline = Date.now() + 60_000;
    while (String(value.state) !== "ready") {
      assert.equal(value.errorReason, null);
      if (Date.now() >= deadline) throw new Error("Volume did not become ready");
      await new Promise((resolve) => setTimeout(resolve, 250));
      value = await client.volume.get(name);
    }
    return value;
  };
  try {
    progress("create-volume");
    if (scenario === "get-create-concurrency") {
      const outcomes = await Promise.allSettled([client.volume.get(name, true), client.volume.get(name, true)]);
      const successes = outcomes.flatMap((outcome) => outcome.status === "fulfilled" ? [outcome.value] : []);
      assert.ok(successes.length > 0);
      volume = successes[0];
      assert.ok(successes.every((candidate) => candidate.id === volume.id));
      for (const outcome of outcomes) {
        if (outcome.status === "rejected") {
          assert.ok(outcome.reason instanceof DaytonaBadRequestError);
          assert.equal(outcome.reason.statusCode, 400);
        }
      }
      await assert.rejects(client.volume.create(name), (error: any) => error instanceof DaytonaBadRequestError && error.statusCode === 400);
      assert.equal((await client.volume.get(name)).id, volume.id);
      volume = await ready(volume);
      return;
    }
    volume = await ready(await client.volume.create(name));
    progress("get-list");
    assert.equal((await client.volume.get(name, true)).id, volume.id);
    assert.ok((await client.volume.list()).some((candidate) => candidate.id === volume.id));
    for (let index = 0; index < (scenario === "delete-mounted" ? 1 : 2); index += 1) {
      progress(`create-mounted-${index}`);
      const subpath = scenario === "subpath-filesystem" && index === 1 ? "nested" : undefined;
      if (subpath) {
        await sandboxes[0].fs.createFolder(`${mountPath}/nested`, "755");
        await sandboxes[0].fs.uploadFile(Buffer.from("outside-subtree"), `${mountPath}/sibling`);
      }
      sandboxes.push(await client.create({ volumes: [{ volumeId: volume.id, mountPath, ...(subpath ? { subpath } : {}) }] }));
    }
    if (scenario === "delete-mounted") {
      progress("delete-mounted");
      await sandboxes[0].fs.uploadFile(Buffer.from("retained"), `${mountPath}/marker`);
      await assert.rejects(client.volume.delete(volume), (error: any) => error instanceof DaytonaConflictError && error.statusCode === 409);
      assert.equal(String((await client.volume.get(name)).state), "ready");
      assert.equal((await sandboxes[0].fs.downloadFile(`${mountPath}/marker`)).toString(), "retained");
      return;
    }
    const [first, second] = sandboxes;
    const root = scenario === "subpath-filesystem" ? `${mountPath}/nested` : mountPath;
    const source = `${root}/bytes`;
    const target = `${root}/renamed`;
    const secondSource = `${mountPath}/bytes`;
    const secondTarget = `${mountPath}/renamed`;
    const byteRange = Buffer.from(Array.from({ length: 256 }, (_, index) => index));
    const payload = Buffer.concat([byteRange, byteRange, byteRange, byteRange, Buffer.from([0, 255])]);
    if (scenario === "subpath-filesystem") {
      await assert.rejects(second.fs.downloadFile(`${mountPath}/sibling`), DaytonaNotFoundError);
    }
    progress("cross-sandbox-write");
    await first.fs.uploadFile(payload, source);
    await eventually(async () => assert.deepEqual(await second.fs.downloadFile(secondSource), payload));
    progress("cross-sandbox-rename");
    await second.fs.moveFiles(secondSource, secondTarget);
    await eventually(async () => assert.deepEqual(await first.fs.downloadFile(target), payload));
    await eventually(async () => assert.rejects(first.fs.downloadFile(source), DaytonaNotFoundError));
    progress("cross-sandbox-unlink");
    await first.fs.deleteFile(target);
    await eventually(async () => assert.rejects(second.fs.downloadFile(secondTarget), DaytonaNotFoundError));
    if (scenario === "subpath-filesystem") {
      assert.equal((await first.fs.downloadFile(`${mountPath}/sibling`)).toString(), "outside-subtree");
    }
    await assert.rejects(client.volume.get(`missing-${randomUUID()}`), DaytonaNotFoundError);
  } catch (error) { primary = error; throw error; }
  finally {
    const cleanupErrors: unknown[] = [];
    for (const sandbox of sandboxes.reverse()) {
      try { await client.delete(sandbox); } catch (error) { cleanupErrors.push(error); }
    }
    if (volume) {
      try {
        volume = await ready(volume);
        await (cleanupVolume ? cleanupVolume(volume.id) : client.volume.delete(volume));
      }
      catch (error) { cleanupErrors.push(error); }
    }
    try { await client[Symbol.asyncDispose](); } catch (error) { cleanupErrors.push(error); }
    if (cleanupErrors.length) throw new Error("Owned Daytona volume resources did not clean up", { cause: primary ?? cleanupErrors[0] });
  }
}

export async function main(): Promise<void> {
  await withLiveRouter(["DAYTONA_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
    const daytonaKey = process.env.DAYTONA_API_KEY;
    for (const scenario of ["shared-volume-filesystem", "subpath-filesystem", "get-create-concurrency", "delete-mounted"] as const) {
      for (const [provider, Client, route] of [
        ["daytona", Native, "reference"], ["daytona", Shim, "native"], ["arker", Shim, "arker"],
      ] as const) {
        await setProvider(provider);
        if (provider === "arker") delete process.env.DAYTONA_API_KEY;
        else process.env.DAYTONA_API_KEY = daytonaKey;
        let outcome = "failed";
        let stage = "setup";
        try {
          await sharedFilesystemProgram(
            Client,
            provider === "arker" ? (id) => new Arker().deleteFilesystem(id).then(() => undefined) : undefined,
            (value) => { stage = value; },
            scenario,
          );
          outcome = "passed";
        } finally {
          const apis = scenario === "get-create-concurrency"
            ? ["Daytona.volume.create", "Daytona.volume.get"]
            : scenario === "subpath-filesystem"
              ? [...APIS, "Sandbox.fs.createFolder"]
              : scenario === "delete-mounted"
                ? [...APIS.filter((api) => !["Sandbox.fs.moveFiles", "Sandbox.fs.deleteFile"].includes(api)), "Daytona.volume.delete"]
                : APIS;
          await recordEvidence("daytona", route, scenario, apis, outcome);
          console.log(`${outcome.toUpperCase()} Daytona ${scenario} ${route} at ${stage}`);
        }
      }
    }
  });
}

if (import.meta.main) void main();
