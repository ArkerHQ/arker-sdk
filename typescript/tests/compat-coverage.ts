import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import guestSource from "../src/compat/guest-process.generated.js";
import { verifiedScenarios } from "../scripts/compat-evidence.js";
import { Template } from "../src/e2b.js";
import { Template as NativeTemplate } from "e2b";
import { Image as DaytonaImage } from "../src/daytona.js";
import { Image as NativeDaytonaImage } from "@daytona/sdk";
import { Image as ModalImage } from "../src/modal.js";
import { Image as NativeModalImage } from "modal";

assert.equal(Template, NativeTemplate);
assert.equal(DaytonaImage, NativeDaytonaImage);
assert.equal(ModalImage, NativeModalImage);
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
console.log("PASS compatibility coverage evidence validation");
