import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModalClient as Native, ContainerProcess } from "modal";
import { ModalClient as Shim } from "../src/modal.js";
import { Arker } from "../src/index.js";
import { recordEvidence } from "../scripts/compat-evidence.js";

async function program(
  Client: typeof Native | typeof Shim,
  id: string,
  terminated: () => void,
) {
  const client = new Client();
  try {
    const sandbox = await client.sandboxes.fromId(id);
    try {
      const argv = await sandbox.exec([
        "sh",
        "-c",
        'printf "%s:%s\\n" "$0" "$1"',
        "argv0",
        "argument space",
      ]);
      assert.ok(argv instanceof ContainerProcess);
      assert.equal(await argv.stdout.readText(), "argv0:argument space\n");
      assert.equal(await argv.wait(), 0);
      const changed = await sandbox.exec(
        ["sh", "-c", 'printf "%s:" "$VALUE"; pwd'],
        { workdir: "/tmp", env: { VALUE: "native" } },
      );
      assert.equal(await changed.stdout.readText(), "native:/tmp\n");
      assert.equal(await changed.wait(), 0);
      const isolated = await sandbox.exec([
        "sh",
        "-c",
        'printf "%s:" "${VALUE-unset}"; pwd',
      ]);
      assert.equal(await isolated.stdout.readText(), "unset:/\n");
      assert.equal(await isolated.wait(), 0);
      const payload = Uint8Array.from(
        { length: 262144 },
        (_, index) => index % 256,
      );
      const binary = await sandbox.exec(
        ["sh", "-c", "cat; printf diagnostic >&2; exit 17"],
        { mode: "binary" },
      );
      assert.ok(binary instanceof ContainerProcess);
      await binary.stdin.writeBytes(payload);
      await binary.stdin.close();
      const [stdout, stderr, code] = await Promise.all([
        binary.stdout.readBytes(),
        binary.stderr.readText(),
        binary.wait(),
      ]);
      assert.deepEqual(stdout, payload);
      assert.equal(stderr, "diagnostic");
      assert.equal(code, 17);
      const cancel = await sandbox.exec(["sh", "-c", "printf ready; cat"]);
      const reader = cancel.stdout.getReader();
      let ready = "";
      while (ready.length < 5) {
        const chunk = await reader.read();
        assert.equal(chunk.done, false);
        ready += chunk.value;
      }
      assert.equal(ready, "ready");
      await reader.cancel();
      await cancel.stdin.close();
      assert.equal(await cancel.wait(), 0);
    } finally {
      await sandbox.terminate();
      terminated();
    }
  } finally {
    client.close();
  }
}

if (
  !process.env.MODAL_TOKEN_ID ||
  !process.env.MODAL_TOKEN_SECRET ||
  !process.env.ARKER_API_KEY
)
  throw new Error("Live Modal and Arker credentials required");
const directory = await mkdtemp(join(tmpdir(), "compat-modal-process-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
const credentials = {
  id: process.env.MODAL_TOKEN_ID,
  secret: process.env.MODAL_TOKEN_SECRET,
};
const native = new Native();
try {
  const app = await native.apps.fromName("arker-compat-image-test", {
    createIfMissing: true,
  });
  const image = await native.images.fromRegistry("python:3.12-slim").build(app);
  for (const [provider, Client, route] of [
    ["modal", Native, "reference"],
    ["modal", Shim, "native"],
    ["arker", Shim, "arker"],
  ] as const) {
    const path = join(directory, provider + ".json");
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        existing: { strategy: "priority", order: [provider] },
        create: { strategy: "priority", order: [provider] },
      }),
    );
    process.env.COMPUTESDK_ROUTER_CONFIG = path;
    const fixture =
      provider === "modal"
        ? await native.sandboxes.create(app, image, { blockNetwork: true })
        : await new Arker().fork({
            image: "python:3.12-slim",
            policies: {
              policies: [
                { type: "inbound", action: "deny" },
                { type: "outbound", action: "deny" },
              ],
            },
          });
    const id = "sandboxId" in fixture ? fixture.sandboxId : fixture.id;
    if (provider === "arker") {
      delete process.env.MODAL_TOKEN_ID;
      delete process.env.MODAL_TOKEN_SECRET;
    }
    let deleted = false;
    let outcome = "failed";
    try {
      await program(Client, id, () => {
        deleted = true;
      });
      outcome = "passed";
      console.log(`PASS Modal process program: ${route}`);
    } finally {
      if (!deleted) {
        if ("terminate" in fixture) await fixture.terminate();
        else await fixture.delete();
      }
      await recordEvidence(
        "modal",
        route,
        "from-id-native-streaming-process",
        [
          "ModalClient.sandboxes.fromId",
          "Sandbox.exec",
          "Sandbox.terminate",
          "ContainerProcess.wait",
          "ContainerProcess.stdin.writeBytes",
          "ContainerProcess.stdin.close",
          "ContainerProcess.stdout.readText",
          "ContainerProcess.stdout.readBytes",
          "ContainerProcess.stdout.getReader",
          "ContainerProcess.stderr.readText",
        ],
        outcome,
      );
    }
  }
} finally {
  process.env.MODAL_TOKEN_ID = credentials.id;
  process.env.MODAL_TOKEN_SECRET = credentials.secret;
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  native.close();
  await rm(directory, { recursive: true });
}
