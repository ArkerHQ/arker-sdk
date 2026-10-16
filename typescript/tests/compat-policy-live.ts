import assert from "node:assert/strict";

import { daytona as createDaytonaProvider } from "@computesdk/daytona";
import { e2b as createE2BProvider } from "@computesdk/e2b";
import { modal as createModalProvider } from "@computesdk/modal";
import { compute, type InstanceInterface as Instance } from "computesdk";

import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";

type Box = { run(command: string): Promise<{ exitCode: number }>; destroy(): Promise<void> };

function native(instance: Instance): Box {
  return { run: (command) => instance.runCommand(command), destroy: () => instance.destroy() };
}

async function assertBlocked(name: string, box: Box): Promise<void> {
  try {
    const result = await box.run("curl -fsS --max-time 5 https://example.com >/dev/null");
    assert.notEqual(result.exitCode, 0, `${name}: outbound HTTPS unexpectedly succeeded`);
    console.log(`PASS ${name} outbound block`);
  } finally {
    await box.destroy();
  }
}

const e2bNative = await compute({ provider: createE2BProvider({ apiKey: process.env.E2B_API_KEY }) }).instances.create({
  timeout: 300_000,
  allowInternetAccess: false,
});
const e2bArker = await E2BSandbox.create(undefined, { allowInternetAccess: false } as never);
await assertBlocked("e2b/native", native(e2bNative));
await assertBlocked("e2b/arker", {
  run: (command) => e2bArker.commands.run(command),
  destroy: () => e2bArker.kill(),
});

const daytonaNative = await compute({ provider: createDaytonaProvider({ apiKey: process.env.DAYTONA_API_KEY }) }).instances.create({
  timeout: 300_000,
  networkBlockAll: true,
});
const daytonaArker = await new Daytona({ arker: { apiKey: process.env.ARKER_API_KEY } }).create({ networkBlockAll: true });
await assertBlocked("daytona/native", native(daytonaNative));
await assertBlocked("daytona/arker", {
  run: async (command) => daytonaArker.process.exec(command),
  destroy: () => daytonaArker.delete(),
});

const modalNative = await compute({ provider: createModalProvider({
  tokenId: process.env.MODAL_TOKEN_ID,
  tokenSecret: process.env.MODAL_TOKEN_SECRET,
}) }).instances.create({ timeout: 300_000, blockNetwork: true });
const modalArker = await new ModalClient({ arker: { apiKey: process.env.ARKER_API_KEY } }).sandboxes.create({ blockNetwork: true });
await assertBlocked("modal/native", native(modalNative));
await assertBlocked("modal/arker", {
  run: async (command) => {
    const process = await modalArker.exec(["sh", "-lc", command]);
    return { exitCode: await process.wait() };
  },
  destroy: () => modalArker.terminate(),
});

console.log("PASS compatibility policy matrix");
