import assert from "node:assert/strict";
import test from "node:test";
import { e2bForkMetadata } from "../src/compat/e2b-metadata.js";

for (const [request, source] of [
  [{ source_vm_id: "source-id" }, { kind: "vm_id", value: "source-id" }],
  [{ source_vm_name: "source-name" }, { kind: "vm_name", value: "source-name" }],
] as const) {
  test(`metadata records the resolved ${source.kind} and clears omitted user metadata`, () => {
    assert.deepEqual(e2bForkMetadata(request, undefined), { arker_e2b: { version: 1, envd_version: "0.10.2", metadata: {}, source } });
    const metadata = { arker_e2b: "caller-owned-string", purpose: "test" };
    assert.deepEqual(e2bForkMetadata(request, metadata), { arker_e2b: { version: 1, envd_version: "0.10.2", metadata, source } });
    assert.deepEqual(metadata, { arker_e2b: "caller-owned-string", purpose: "test" });
  });
}

test("metadata rejects non-string values and fabricated source identity", () => {
  for (const metadata of [null, [], "text", { count: 1 }, { nested: {} }]) {
    assert.throws(() => e2bForkMetadata({ source_vm_id: "source" }, metadata), /string-to-string/);
  }
  assert.throws(() => e2bForkMetadata({ image: "image" }, {}), /actual VM source/);
});
