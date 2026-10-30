import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { Daytona as Native } from "@daytona/sdk";
import { Daytona as Shim } from "../src/daytona.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";

export async function program(Client: typeof Native | typeof Shim, progress: (stage: string) => void = () => {}) {
  progress("client-init");
  const client = new Client();
  let sandbox: Awaited<ReturnType<typeof client.create>> | undefined;
  let primary: unknown;
  let failed = false;
  try {
    progress("create");
    sandbox = await client.create();
      const fs = sandbox.fs;
      const directory = `compat-files-${randomUUID()}`;
      progress("create-folder");
      await fs.createFolder(directory, "700");
      const path = `${directory}/bytes`;
      const payload = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
      progress("upload");
      await fs.uploadFile(payload, path);
      progress("download");
      assert.deepEqual(await fs.downloadFile(path), payload);
      progress("metadata");
      const info = await fs.getFileDetails(path);
      assert.equal(info.size, 256);
      progress("identity");
      assert.equal(info.owner, (await sandbox.process.executeCommand("id -u")).result.trim());
      progress("permissions");
      await fs.setFilePermissions(path, { mode: "640" });
      progress("overwrite");
      await fs.uploadFile(Buffer.from("short"), path);
      assert.equal((await fs.getFileDetails(path)).permissions, "0640");
      progress("move");
      await fs.moveFiles(path, `${directory}/moved`);
      progress("list");
      assert.deepEqual((await fs.listFiles(directory)).map(v => v.name), ["moved"]);
      progress("delete-folder");
      await fs.deleteFile(directory, true);
  } catch (error) { failed = true; primary = error; }
  let cleanup: unknown;
  for (const action of [
    ...(sandbox ? [{ stage: "cleanup-delete", run: () => client.delete(sandbox as any) }] : []),
    { stage: "cleanup-dispose", run: () => client[Symbol.asyncDispose]() },
  ]) {
    try { if (!failed && !cleanup) progress(action.stage); await action.run(); }
    catch (error) { cleanup ??= error; }
  }
  if (failed) {
    if (cleanup) console.error("Owned-resource cleanup also failed; verify deletion independently.");
    throw primary;
  }
  if (cleanup) throw cleanup;
}

export async function main() {
  await withLiveRouter(["DAYTONA_API_KEY", "ARKER_API_KEY"], async ({ setProvider }) => {
    const key = process.env.DAYTONA_API_KEY;
    for (const [provider, Client, route] of [["daytona", Native, "reference"], ["daytona", Shim, "native"], ["arker", Shim, "arker"]] as const) {
      await setProvider(provider);
      if (provider === "arker") delete process.env.DAYTONA_API_KEY;
      else process.env.DAYTONA_API_KEY = key;
      let outcome = "failed";
      let failure: unknown;
      let stage = "setup";
      try { await program(Client, value => { stage = value; }); outcome = "passed"; }
      catch (error) { failure = error; }
      try {
        await recordEvidence("daytona", route, "buffered-filesystem", [
          "Sandbox.fs.createFolder", "Sandbox.fs.uploadFile", "Sandbox.fs.downloadFile", "Sandbox.fs.getFileDetails",
          "Sandbox.fs.listFiles", "Sandbox.fs.setFilePermissions", "Sandbox.fs.moveFiles", "Sandbox.fs.deleteFile",
        ], outcome);
      } catch (error) { if (!failure) { failure = error; stage = "evidence"; } }
      if (failure) {
        const allowed = new Set(["Error", "TypeError", "AssertionError", "CompatibilityCreationNotStartedError", "UnsupportedCompatibilityOption", "DaytonaError", "DaytonaForbiddenError", "DaytonaNotFoundError", "DaytonaBadRequestError"]);
        const name = failure instanceof Error ? failure.constructor.name : "UnknownError";
        console.error(`Daytona filesystem ${route} failed at ${stage}: ${allowed.has(name) ? name : "ProviderError"}.`);
        throw failure;
      }
    }
  });
}

if (import.meta.main) {
  void main().catch(() => {
    // Never print raw provider errors: they can contain request credentials.
    console.error("Daytona filesystem scenario failed; inspect failed route evidence. Raw provider error suppressed.");
    process.exitCode = 1;
  });
}
