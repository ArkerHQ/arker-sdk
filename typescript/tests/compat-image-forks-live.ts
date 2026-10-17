import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import type { SandboxInterface } from "computesdk";

import { type VM } from "../src/index.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";

if (!process.env.ARKER_API_KEY) throw new Error("ARKER_API_KEY is required");
const token = randomBytes(24).toString("hex");
const policies = {
  secrets: { COMPAT_HOST_TOKEN: token },
  policies: [{ type: "outbound" as const, action: "deny" as const }],
};
const owned: SandboxInterface[] = [];
const vm = (box: SandboxInterface) => (box as SandboxInterface & { getInstance(): VM }).getInstance();
const config = { apiKey: process.env.ARKER_API_KEY, baseUrl: process.env.ARKER_BASE_URL };
try {
  const parent = await createArkerComputeProvider({ ...config, policies }).sandbox.create({ image: "ubuntu:24.04" });
  owned.push(parent);
  assert.equal((await parent.runCommand("printf image-ready")).stdout, "image-ready");
  assert.deepEqual((await vm(parent).getPolicies()).secrets, { COMPAT_HOST_TOKEN: "***" });
  const connect = "timeout 10 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'";
  await vm(parent).setPolicies({ policies: [{ type: "outbound", action: "allow" }] });
  assert.equal((await parent.runCommand(connect, { timeout: 15_000 })).exitCode, 0);
  await vm(parent).setPolicies(policies);
  const directory = "/tmp/compat-directory-parity";
  await parent.filesystem.mkdir(directory);
  const names = ["line\nbreak", "tab\tname", "-leading", ".hidden", "quote'file", "back\\slash"];
  for (const name of names) await parent.filesystem.writeFile(`${directory}/${name}`, "hello");
  assert.equal((await parent.runCommand(`ln -s missing ${directory}/dangling`)).exitCode, 0);
  const entries = await parent.filesystem.readdir(directory);
  assert.deepEqual(entries.map(entry => entry.name).sort(), [...names, "dangling"].sort());
  for (const entry of entries.filter(entry => names.includes(entry.name))) assert.equal(entry.size, 5);
  await parent.filesystem.writeFile("/tmp/compat-image-marker", "parent");
  const provider = createArkerComputeProvider({ ...config, sourceVmId: parent.sandboxId });
  for (let index = 0; index < 3; index++) {
    const child = await provider.sandbox.create();
    owned.push(child);
    assert.equal(await child.filesystem.readFile("/tmp/compat-image-marker"), "parent");
    const stored = await vm(child).getPolicies();
    assert.deepEqual(stored.secrets, { COMPAT_HOST_TOKEN: "***" });
    assert.ok(!JSON.stringify(stored).includes(token));
    assert.equal((await child.runCommand("printenv COMPAT_HOST_TOKEN")).exitCode, 1);
    assert.notEqual((await child.runCommand(connect, { timeout: 15_000 })).exitCode, 0);
  }
  assert.equal(new Set(owned.map(box => box.sandboxId)).size, 4);
  await owned[1]!.filesystem.writeFile("/tmp/compat-image-marker", "changed");
  for (const box of [owned[0]!, ...owned.slice(2)]) {
    assert.equal(await box.filesystem.readFile("/tmp/compat-image-marker"), "parent");
  }
  console.log("PASS image import, three independent forks, policy secrets, controlled egress denial and literal directory names");
} finally {
  const cleanup = await Promise.allSettled(owned.map(box => box.destroy()));
  assert.ok(cleanup.every(result => result.status === "fulfilled"), "owned sandbox cleanup failed");
}
