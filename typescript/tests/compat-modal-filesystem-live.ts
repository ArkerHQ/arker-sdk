import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ModalClient as Native,
  SandboxFilesystemDirectoryNotEmptyError,
  SandboxFilesystemNotFoundError,
} from "modal";
import { ModalClient as Shim } from "../src/modal.js";
import { Arker } from "../src/index.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { withLiveRouter } from "./compat-live-router.js";

async function program(Client: typeof Native | typeof Shim, id: string) {
  const client = new Client();
  const local = await mkdtemp(join(tmpdir(), "modal-fs-copy-"));
  try {
    const sandbox = await client.sandboxes.fromId(id);
    const filesystem = sandbox.filesystem;
    const path = "/tmp/arker-modal-filesystem-differential";
    await filesystem.makeDirectory(path);
    const payload = Uint8Array.from(
      { length: 65536 },
      (_, index) => index % 256,
    );
    await filesystem.writeBytes(payload, path + "/binary");
    assert.deepEqual(await filesystem.readBytes(path + "/binary"), payload);
    await filesystem.writeText("hello λ\n", path + "/nested/text");
    assert.equal(await filesystem.readText(path + "/nested/text"), "hello λ\n");
    const info = await filesystem.stat(path + "/binary");
    assert.equal(info.type, "file");
    assert.equal(info.size, payload.length);
    assert.equal(info.owner, "root");
    assert.equal(info.group, "root");
    assert.equal(info.permissions, "0644");
    assert.deepEqual(
      (await filesystem.listFiles(path)).map((entry) => entry.name),
      ["binary", "nested"],
    );
    const target = join(local, "nested/binary");
    await filesystem.copyToLocal(path + "/binary", target);
    assert.deepEqual(new Uint8Array(await readFile(target)), payload);
    await filesystem.copyFromLocal(target, path + "/uploaded");
    assert.deepEqual(await filesystem.readBytes(path + "/uploaded"), payload);
    await assert.rejects(
      filesystem.copyToLocal(path + "/missing", target),
      SandboxFilesystemNotFoundError,
    );
    assert.deepEqual(new Uint8Array(await readFile(target)), payload);
    await assert.rejects(
      filesystem.remove(path),
      SandboxFilesystemDirectoryNotEmptyError,
    );
    await filesystem.remove(path, { recursive: true });
    await assert.rejects(filesystem.stat(path), SandboxFilesystemNotFoundError);
  } finally {
    client.close();
    await rm(local, { recursive: true, force: true });
  }
}

await withLiveRouter(["MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "ARKER_API_KEY"], async ({ setProvider }) => {
  const native = new Native();
  try {
    const app = await native.apps.fromName("arker-compat-image-test", {
      createIfMissing: false,
    });
    const image = await native.images.fromRegistry("python:3.12-slim").build(app);
    for (const [provider, Client, route] of [
      ["modal", Native, "reference"],
      ["modal", Shim, "native"],
      ["arker", Shim, "arker"],
    ] as const) {
      await setProvider(provider);
      const replayKey = "modal-filesystem-" + randomUUID();
      const fixture = await (provider === "modal"
        ? native.sandboxes.create(app, image, {
            blockNetwork: true,
            timeoutMs: 180000,
          })
        : new Arker()
            .fork({
              image: "python:3.12-slim",
              idempotencyKey: replayKey,
              policies: {
                policies: [
                  { type: "inbound", action: "deny" },
                  { type: "outbound", action: "deny" },
                ],
              },
            })
            .catch((error) => {
              throw new Error(
                `Fork did not return an owned VM; reconcile replay key ${replayKey} before another attempt`,
                { cause: error },
              );
            }));
      const id = "sandboxId" in fixture ? fixture.sandboxId : fixture.id;
      let outcome: "passed" | "failed" = "failed";
      try {
        await program(Client, id);
        outcome = "passed";
        console.log(`PASS Modal filesystem program: ${route}`);
      } finally {
        try {
          if ("terminate" in fixture) await fixture.terminate();
          else await fixture.delete();
        } catch (error) {
          outcome = "failed";
          throw error;
        } finally {
          await recordEvidence(
            "modal",
            route,
            "from-id-filesystem-binary-metadata-local-copy",
            [
              "ModalClient.sandboxes.fromId",
              "Sandbox.filesystem",
              ...[
                "makeDirectory",
                "writeBytes",
                "readBytes",
                "writeText",
                "readText",
                "stat",
                "listFiles",
                "copyToLocal",
                "copyFromLocal",
                "remove",
              ].map((name) => "Sandbox.filesystem." + name),
            ],
            outcome,
          );
        }
      }
    }
  } finally {
    native.close();
  }
});
