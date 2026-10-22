import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import guestSource from "../src/compat/guest-process.generated.js";
import { captureEvidenceSnapshot, implementationFingerprint, runtimeInfo, verifiedScenarios } from "../scripts/compat-evidence.js";
import { Template } from "../src/e2b.js";
import { Template as NativeTemplate } from "e2b";
import { Image as DaytonaImage } from "../src/daytona.js";
import { Image as NativeDaytonaImage } from "@daytona/sdk";
import { Image as ModalImage } from "../src/modal.js";
import { Image as NativeModalImage } from "modal";

assert.equal(Template, NativeTemplate);
assert.equal(DaytonaImage, NativeDaytonaImage);
assert.equal(ModalImage, NativeModalImage);
const incompleteEvidenceEnvironment: NodeJS.ProcessEnv = { ...process.env, COMPAT_COVERAGE_EVIDENCE: "/unused-evidence.jsonl" };
delete incompleteEvidenceEnvironment.COMPAT_COVERAGE_RUN;
assert.throws(() => execFileSync("bun", ["--eval", 'await import("./scripts/compat-evidence.ts")'], {
  env: incompleteEvidenceEnvironment, stdio: "pipe",
}), /COMPAT_COVERAGE_RUN/);
const inventory = JSON.parse(execFileSync("bun", ["scripts/compat-coverage.ts"], { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }));
assert.equal(inventory.complete_inventory, true, JSON.stringify(inventory.diagnostics));
assert.equal(inventory.selected_scope, "expanded");
assert.ok(inventory.apis.some((row: { api: string }) => row.api.startsWith("App.")));
const coreInventory = JSON.parse(execFileSync("bun", ["scripts/compat-coverage.ts", "--scope", "core-sandbox"], { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }));
assert.equal(coreInventory.selected_scope, "core-sandbox");
assert.ok(coreInventory.scope_exclusions.some((row: { api: string }) => row.api.startsWith("App.")));
assert.ok(!coreInventory.apis.some((row: { api: string }) => row.api.startsWith("App.")));
for (const method of ["send", "close"]) {
  const input = coreInventory.apis.find((row: { provider: string; api: string }) => row.provider === "e2b" && row.api === `CommandInputHandle.${method}`);
  assert.ok(input, `Returned E2B input handle ${method} must be inventoried`);
  assert.equal(input.status, "unverified", "a native-only handle export is not Arker parity evidence");
}
for (const prefix of ["Sandbox.", "Image.", "Volume.", "Secret.", "DriveInstance.", "ModalClient.cloudBucketMounts.", "ModalClient.cpClient.sandbox"]) {
  assert.ok(coreInventory.apis.some((row: { api: string }) => row.api.startsWith(prefix)), `${prefix} stays in core scope`);
}
assert.throws(() => execFileSync("bun", ["scripts/compat-coverage.ts", "--scope", "unknown"], { stdio: "pipe" }));
for (const name of ["DriveInstance", "VolumeInstance", "Snapshot", "ImageInstance"]) {
  const entries = inventory.apis.filter((row: { provider: string; api: string }) => row.provider === "blaxel" && row.api.startsWith(`${name}.`));
  assert.ok(entries.length > 0, `Blaxel ${name} must be inventoried`);
  assert.ok(entries.every((row: { status: string }) => row.status === "unverified"), "native-only exports are not Arker parity evidence");
}
assert.equal(guestSource, await readFile(new URL("../../python/src/arker/_guest_process.py", import.meta.url), "utf8"));

const event = (route: string, options: Record<string, unknown> = {}) => ({
  run: "run-1", scenario: "stdin", route, outcome: "passed", apis: ["process.exec"],
  vendor_version: "1", implementation_fingerprint: "current", ...options,
});
const native = [event("reference"), event("native")];
const complete = [...native, event("arker")];
assert.deepEqual(verifiedScenarios("process.exec", native, "1", "current"), []);
assert.deepEqual(verifiedScenarios("process.exec", complete, "1", "current"), ["run-1:stdin"]);
assert.deepEqual(verifiedScenarios("process.exec", complete, "2", "current"), []);
assert.deepEqual(verifiedScenarios("process.exec", complete, "1", "different"), []);
assert.deepEqual(verifiedScenarios("process.exec", [...complete, event("arker", { outcome: "failed" })], "1", "current"), []);
assert.deepEqual(verifiedScenarios("process.exec", [...native, event("arker", { run: "run-2" })], "1", "current"), []);
assert.deepEqual(verifiedScenarios("process.kill", complete, "1", "current"), []);
assert.deepEqual(verifiedScenarios("process.exec", [...complete, event("arker", { outcome: "failed", apis: [] })], "1", "current"), []);

assert.deepEqual(verifiedScenarios("process.exec", [
  event("reference", { run: "a:b", scenario: "c" }),
  event("native", { run: "a:b", scenario: "c" }),
  event("arker", { run: "a", scenario: "b:c" }),
], "1", "current"), []);
for (const options of [{ run: "" }, { scenario: null }, { route: "other" }, { outcome: "skipped" }, { apis: "process.exec" }]) {
  assert.throws(() => verifiedScenarios("process.exec", [event("arker", options)], "1", "current"), /Invalid compatibility evidence/);
}
const fingerprint = await implementationFingerprint();
assert.equal(runtimeInfo().node, process.versions.node);
const snapshots = ["before", "after"];
const finishSnapshot = captureEvidenceSnapshot(async () => snapshots.shift()!);
assert.deepEqual(await finishSnapshot(), { fingerprint: "before", changed: true });
assert.deepEqual(await captureEvidenceSnapshot(async () => "stable")(), { fingerprint: "stable", changed: false });
for (const name of ["compat-evidence.ts", "compat-coverage.ts", "compat-trace.ts", "compat-blaxel-import-live.ts", "http-service.py", "api-scope.json", "dist/index.mjs"]) {
  const changed = await implementationFingerprint(async (path) => {
    const content = await readFile(path);
    return path.endsWith(`/${name}`) ? Buffer.concat([content, Buffer.from("\n// changed harness\n")]) : content;
  });
  assert.notEqual(changed, fingerprint, `${name} must invalidate existing evidence`);
}
console.log("PASS compatibility coverage evidence validation");
