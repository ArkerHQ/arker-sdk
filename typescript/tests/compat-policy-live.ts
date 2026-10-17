import assert from "node:assert/strict";

import { daytona as createDaytonaProvider } from "@computesdk/daytona";
import { e2b as createE2BProvider } from "@computesdk/e2b";
import { modal as createModalProvider } from "@computesdk/modal";
import { compute, type InstanceInterface as Instance } from "computesdk";

import { Daytona } from "../src/daytona.js";
import { Sandbox as E2BSandbox } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";

type Box = { run(command: string): Promise<{ exitCode: number }>; destroy(): Promise<void> };
const unverified: string[] = [];

function native(instance: Instance): Box {
  return { run: (command) => instance.runCommand(command), destroy: () => instance.destroy() };
}

async function assertBlocked(name: string, box: Box): Promise<void> {
  try {
    assert.equal((await box.run("command -v curl >/dev/null")).exitCode, 0, `${name}: curl is required`);
    const result = await box.run("curl -fsS --max-time 5 https://example.com >/dev/null");
    assert.notEqual(result.exitCode, 0, `${name}: outbound HTTPS unexpectedly succeeded`);
    console.log(`PASS ${name} outbound block`);
  } finally {
    await box.destroy();
  }
}

async function assertAllowlist(name: string, box: Box): Promise<void> {
  try {
    const allowed = await box.run("curl -fsS --max-time 10 https://example.com >/dev/null");
    assert.equal(allowed.exitCode, 0, `${name}: allowlisted HTTPS destination failed`);
    const blocked = await box.run("curl -fsS --max-time 5 https://www.google.com >/dev/null");
    assert.notEqual(blocked.exitCode, 0, `${name}: non-allowlisted HTTPS destination unexpectedly succeeded`);
    console.log(`PASS ${name} outbound allowlist`);
  } finally {
    await box.destroy();
  }
}

const e2bNative = await compute({ provider: createE2BProvider({ apiKey: process.env.E2B_API_KEY }) }).instances.create({
  timeout: 300_000,
  allowInternetAccess: false,
});
await assertBlocked("e2b/native", native(e2bNative));
const e2bArker = await E2BSandbox.create(undefined, { allowInternetAccess: false } as never);
await assertBlocked("e2b/arker", {
  run: (command) => e2bArker.commands.run(command),
  destroy: () => e2bArker.kill(),
});

const daytonaNative = await compute({ provider: createDaytonaProvider({ apiKey: process.env.DAYTONA_API_KEY }) }).instances.create({
  timeout: 300_000,
  networkBlockAll: true,
});
await assertBlocked("daytona/native", native(daytonaNative));
const daytonaArker = await new Daytona({ arker: { apiKey: process.env.ARKER_API_KEY } }).create({ networkBlockAll: true });
await assertBlocked("daytona/arker", {
  run: async (command) => daytonaArker.process.exec(command),
  destroy: () => daytonaArker.delete(),
});

try {
  const daytonaNativeAllowlist = await compute({ provider: createDaytonaProvider({ apiKey: process.env.DAYTONA_API_KEY }) }).instances.create({
    timeout: 300_000,
    domainAllowList: "example.com",
  });
  await assertAllowlist("daytona/native", native(daytonaNativeAllowlist));
} catch (error) {
  if (!String(error).includes("Network access is restricted")) throw error;
  unverified.push("daytona/native outbound allowlist: supplied workspace forbids per-sandbox allowlist overrides");
  console.log(`UNVERIFIED ${unverified.at(-1)}`);
}
const daytonaArkerAllowlist = await new Daytona({ arker: { apiKey: process.env.ARKER_API_KEY } }).create({
  domainAllowList: "example.com",
});
await assertAllowlist("daytona/arker", {
  run: async (command) => daytonaArkerAllowlist.process.exec(command),
  destroy: () => daytonaArkerAllowlist.delete(),
});

const modalNative = await compute({ provider: createModalProvider({
  tokenId: process.env.MODAL_TOKEN_ID,
  tokenSecret: process.env.MODAL_TOKEN_SECRET,
}) }).instances.create({ timeout: 300_000, blockNetwork: true });
await assertBlocked("modal/native", native(modalNative));
const modalArker = await new ModalClient({ arker: { apiKey: process.env.ARKER_API_KEY } }).sandboxes.create({ blockNetwork: true });
await assertBlocked("modal/arker", {
  run: async (command) => {
    const process = await modalArker.exec(["sh", "-lc", command]);
    return { exitCode: await process.wait() };
  },
  destroy: () => modalArker.terminate(),
});

const modalNativeAllowlist = await compute({ provider: createModalProvider({
  tokenId: process.env.MODAL_TOKEN_ID,
  tokenSecret: process.env.MODAL_TOKEN_SECRET,
}) }).instances.create({ timeout: 300_000, outboundDomainAllowlist: ["example.com"] });
await assertAllowlist("modal/native", native(modalNativeAllowlist));
const modalArkerAllowlist = await new ModalClient({ arker: { apiKey: process.env.ARKER_API_KEY } }).sandboxes.create({
  outboundDomainAllowlist: ["example.com"],
});
await assertAllowlist("modal/arker", {
  run: async (command) => {
    const process = await modalArkerAllowlist.exec(["sh", "-lc", command]);
    return { exitCode: await process.wait() };
  },
  destroy: () => modalArkerAllowlist.terminate(),
});

if (unverified.length) throw new Error(`Policy equivalence is not established: ${unverified.join("; ")}`);
console.log("PASS compatibility policy matrix");
process.exit(0);
