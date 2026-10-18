import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModalClient as Native } from "modal";
import { ModalClient as Shim } from "../src/modal.js";

if (!process.env.MODAL_TOKEN_ID || !process.env.MODAL_TOKEN_SECRET) throw new Error("Modal credentials required");
const directory = await mkdtemp(join(tmpdir(), "compat-modal-image-"));
const config = join(directory, "router.json");
await writeFile(config, JSON.stringify({
  version: 1, existing: { strategy: "priority", order: ["modal"] },
  create: { strategy: "priority", order: ["modal"] },
}));
const native = new Native();
const shim = new Shim({ routerConfigPath: config });
try {
  const app = await shim.apps.fromName("arker-compat-image-test", { createIfMissing: true });
  const image = await shim.images.fromRegistry("alpine:3.22").build(app);
  const outputs: string[] = [];
  for (const [label, client] of [["native", native], ["shim", shim]] as const) {
    const box = await client.sandboxes.create(app, image, { blockNetwork: true });
    try {
      const process = await box.exec(["sh", "-c", "cat /etc/alpine-release; printf image-marker; exit 17"], { stdout: "pipe", stderr: "pipe" });
      outputs.push(await process.stdout.readText());
      assert.equal(await process.wait(), 17);
      assert.match(outputs.at(-1)!, /^3\.22\..*\nimage-marker$/);
      console.log(`PASS Modal custom image and nonzero exit: ${label}`);
    } finally { await box.terminate(); }
  }
  assert.equal(outputs[0], outputs[1]);
} finally {
  native.close();
  shim.close();
  await rm(directory, { recursive: true });
}
