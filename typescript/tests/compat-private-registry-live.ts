import assert from "node:assert/strict";
import type { SandboxInterface } from "computesdk";
import { Arker, ArkerError } from "../src/index.js";
import { createArkerComputeProvider } from "../src/compat/arker-provider.js";

const image = process.env.COMPAT_PRIVATE_REGISTRY_IMAGE;
const username = process.env.COMPAT_REGISTRY_USERNAME;
const password = process.env.COMPAT_REGISTRY_PASSWORD;
assert(image && username && password && process.env.ARKER_API_KEY, "live Arker and private-registry fixture required");
const credentials = { username, password };
const originalFork = Arker.prototype.fork;
let imports = 0;
let forks = 0;
Arker.prototype.fork = async function (options) {
  if (options.image) {
    assert.deepEqual(options.registry_auth, credentials);
    imports++;
  } else {
    assert(options.source_vm_id);
    assert.equal(options.registry_auth, undefined);
    forks++;
  }
  return originalFork.call(this, options);
};
const options = { apiKey: process.env.ARKER_API_KEY, baseUrl: process.env.ARKER_BASE_URL, registryAuth: credentials };
const owned: SandboxInterface[] = [];
try {
  const parent = await createArkerComputeProvider(options).sandbox.create({ image });
  owned.push(parent);
  await parent.filesystem.writeFile("/tmp/private-registry-marker", "parent");
  Arker.prototype.fork = originalFork;
  try {
    const unexpected = await createArkerComputeProvider({
      ...options, registryAuth: { username, password: password + "-wrong" },
    }).sandbox.create({ image });
    owned.push(unexpected);
    assert.fail("cached private image accepted invalid registry credentials");
  } catch (error) {
    assert(error instanceof ArkerError);
    assert.equal(error.status, 422);
    assert.equal(error.code, "operation_failed");
    assert(error.body && "details" in error.body);
    const details = error.body.details;
    assert(details && typeof details === "object" && "reason" in details);
    assert.equal(details.reason, "image_pull_failed");
    assert.equal(error.body.code, "operation_failed");
    if (error.body.code !== "operation_failed") assert.fail("expected typed operation failure");
    assert.equal(error.body.recovery.work, "unknown");
    const diagnostic = JSON.stringify(error.raw);
    assert.ok(!diagnostic.includes(password));
    assert.ok(!diagnostic.includes(Buffer.from(`${username}:${password}-wrong`).toString("base64")));
  }
  Arker.prototype.fork = async function (request) {
    assert(request.source_vm_id);
    assert.equal(request.registry_auth, undefined);
    forks++;
    return originalFork.call(this, request);
  };
  const provider = createArkerComputeProvider({ ...options, sourceVmId: parent.sandboxId });
  for (let index = 0; index < 3; index++) {
    const child = await provider.sandbox.create();
    owned.push(child);
    assert.equal(await child.filesystem.readFile("/tmp/private-registry-marker"), "parent");
    assert.ok(!(await child.runCommand("env")).stdout.includes(password));
  }
  assert.equal(imports, 1);
  assert.equal(forks, 3);
  assert.equal(new Set(owned.map(box => box.sandboxId)).size, 4);
  await owned[1]!.filesystem.writeFile("/tmp/private-registry-marker", "changed");
  for (const box of [parent, ...owned.slice(2)]) {
    assert.equal(await box.filesystem.readFile("/tmp/private-registry-marker"), "parent");
  }
  console.log("PASS private import auth, cached-image bad-auth rejection, three isolated forks and no guest credential env");
} finally {
  Arker.prototype.fork = originalFork;
  const cleanup = await Promise.allSettled(owned.map(box => box.destroy()));
  assert.ok(cleanup.every(result => result.status === "fulfilled"), "owned private-registry sandbox cleanup failed");
}
