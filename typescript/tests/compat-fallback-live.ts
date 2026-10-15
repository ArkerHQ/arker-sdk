import assert from "node:assert/strict";

import { daytona as createDaytonaProvider } from "@computesdk/daytona";
import { e2b as createE2BProvider } from "@computesdk/e2b";
import { modal as createModalProvider } from "@computesdk/modal";
import { blaxel as createBlaxelProvider } from "@computesdk/blaxel";
import { compute } from "computesdk";

import { Daytona } from "../src/daytona.js";
import { Blaxel } from "../src/blaxel.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";

const arkerApiKey = process.env.ARKER_API_KEY;
if (!arkerApiKey) {
  console.log("SKIP compat fallback live: set ARKER_API_KEY plus native provider credentials to run");
  process.exit(0);
}

async function testDaytonaFallback(): Promise<void> {
  const apiKey = process.env.DAYTONA_API_KEY;
  if (!apiKey) {
    console.log("SKIP daytona fallback: set DAYTONA_API_KEY");
    return;
  }

  const native = compute({ provider: createDaytonaProvider({ apiKey }) });
  const nativeSandbox = await native.sandbox.create({});
  try {
    const shim = new Daytona({ apiKey, arker: { apiKey: arkerApiKey } });
    const fetched = await shim.get(nativeSandbox.sandboxId);
    assert.equal(fetched.id, nativeSandbox.sandboxId);
    const result = await fetched.process.exec("printf daytona-fallback");
    assert.equal(result.result, "daytona-fallback");
  } finally {
    await nativeSandbox.destroy();
  }
}

async function testE2BFallback(): Promise<void> {
  if (!process.env.E2B_API_KEY) {
    console.log("SKIP e2b fallback: set E2B_API_KEY");
    return;
  }

  const native = compute({ provider: createE2BProvider({ apiKey: process.env.E2B_API_KEY }) });
  const nativeSandbox = await native.sandbox.create({});
  try {
    const fetched = await E2BSandbox.connect(nativeSandbox.sandboxId);
    assert.equal(fetched.sandboxId, nativeSandbox.sandboxId);
    const result = await fetched.commands.run("printf e2b-fallback");
    assert.equal(result.stdout, "e2b-fallback");
  } finally {
    await nativeSandbox.destroy();
  }
}

async function testModalFallback(): Promise<void> {
  const tokenId = process.env.MODAL_TOKEN_ID;
  const tokenSecret = process.env.MODAL_TOKEN_SECRET;
  if (!tokenId || !tokenSecret) {
    console.log("SKIP modal fallback: set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET");
    return;
  }

  const native = compute({ provider: createModalProvider({ tokenId, tokenSecret }) });
  const nativeSandbox = await native.sandbox.create({});
  try {
    const client = new ModalClient({ tokenId, tokenSecret, arker: { apiKey: arkerApiKey } });
    const fetched = await client.sandboxes.fromId(nativeSandbox.sandboxId);
    assert.equal(fetched.sandboxId, nativeSandbox.sandboxId);
    const proc = await fetched.exec(["sh", "-c", "printf modal-fallback"]);
    assert.equal(await proc.stdout.readText(), "modal-fallback");
  } finally {
    await nativeSandbox.destroy();
  }
}

async function testBlaxelFallback(): Promise<void> {
  const apiKey = process.env.BL_API_KEY;
  const workspace = process.env.BL_WORKSPACE;
  if (!apiKey || !workspace) {
    console.log("SKIP blaxel fallback: set BL_API_KEY and BL_WORKSPACE");
    return;
  }

  const native = compute({ provider: createBlaxelProvider({ apiKey, workspace }) });
  const nativeSandbox = await native.sandbox.create({ timeout: 300_000 });
  try {
    const shim = new Blaxel({ apiKey, workspace, arker: { apiKey: arkerApiKey } });
    const fetched = await shim.get(nativeSandbox.sandboxId);
    assert.equal(fetched.id, nativeSandbox.sandboxId);
    const result = await fetched.process.exec("printf blaxel-fallback");
    assert.equal(result.stdout, "blaxel-fallback");
  } finally {
    await nativeSandbox.destroy();
  }
}

await testDaytonaFallback();
await testE2BFallback();
await testModalFallback();
await testBlaxelFallback();

console.log("PASS compat fallback live");
process.exit(0);
