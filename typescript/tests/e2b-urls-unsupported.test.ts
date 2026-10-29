import assert from "node:assert/strict";
import { test } from "node:test";
import { ArkerError, type VM } from "../src/index.js";
import { E2BFileURLs, prepareFileURLs } from "../src/compat/e2b-urls.js";
import { Sandbox } from "../src/e2b.js";
import { setCompatComputeFactoryForTests } from "../src/compat/runtime.js";

const descriptor = { endpoint: "https://sandbox.example.test", envd_version: "0.10.2", envd_access_token: "test-token" };
function unsupported() {
  return new ArkerError("unsupported_operation", "File URLs are unavailable", 422, {
    code: "unsupported_operation", message: "File URLs are unavailable", timestamp: "2026-01-01T00:00:00Z", request_id: "test",
    request: { kind: "matched", operation_id: "createFileTransferDelegation" }, details: { reason: "feature_not_supported" },
  });
}
function fixture(error: unknown) {
  const calls: unknown[][] = [];
  const vm = { id: "vm", baseUrl: "https://example.test/api", _client: { _request: async (...args: unknown[]) => { calls.push(args); throw error; } } } as unknown as VM;
  return { vm, calls, urls: new E2BFileURLs(vm, undefined, descriptor) };
}

test("unsupported URL methods rethrow exact error locally and preparation retries", async () => {
  const error = unsupported(); const { vm, calls, urls } = fixture(error);
  await prepareFileURLs(vm);
  await assert.rejects(urls.downloadUrl("file"), actual => actual === error);
  await assert.rejects(urls.uploadUrl("file"), actual => actual === error);
  assert.equal(calls.length, 1);
  vm._client._request = (async (...args: unknown[]) => {
    calls.push(args);
    return { redemption_url: "https://regional.example.test/api/v1/vms/vm/file-transfer", context: "b3BhcXVl", signing_key: Buffer.alloc(32, 1).toString("base64url"), operations: ["read", "write"], expires_at: null };
  }) as typeof vm._client._request;
  await prepareFileURLs(vm);
  assert.equal(calls.length, 2);
  assert.match(await urls.downloadUrl("file"), /^https:\/\/regional.example.test\//);
  assert.equal(calls.length, 2);
});

for (const mismatch of [401, 403, 404, 409, 429, 500, 503, "code", "body_code", "reason", "kind", "operation", "request", "details", "body", "untyped", "transport"]) {
  test(`preparation propagates nonmatching failure: ${mismatch}`, async () => {
    let error: any = unsupported();
    if (typeof mismatch === "number") error.status = mismatch;
    else if (mismatch === "code") error.code = "operation_failed";
    else if (mismatch === "body_code") error.body.code = "operation_failed";
    else if (mismatch === "reason") error.body.details.reason = "platform_mismatch";
    else if (mismatch === "kind") error.body.request.kind = "unmatched";
    else if (mismatch === "operation") error.body.request.operation_id = "sync";
    else if (mismatch === "request" || mismatch === "details") error.body[mismatch] = undefined;
    else if (mismatch === "body") error.body = undefined;
    else if (mismatch === "untyped") error = { ...error };
    else error = new Error("transport failed");
    const { vm, calls, urls } = fixture(error);
    await assert.rejects(prepareFileURLs(vm), actual => actual === error);
    assert.equal(calls.length, 1);
    await assert.rejects(urls.downloadUrl("file"), /prepared/);
    assert.equal(calls.length, 1);
  });
}

test("basic create and commands remain available when file URLs are unsupported", async t => {
  const error = unsupported(); const { vm, calls } = fixture(error);
  const factory = Sandbox as unknown as { dataPlane: () => Promise<unknown> };
  const original = factory.dataPlane;
  t.after(() => { factory.dataPlane = original; setCompatComputeFactoryForTests(); });
  const plane = { commands: { run: async (command: string) => command }, files: {}, git: {}, pty: {}, close() {} };
  factory.dataPlane = async () => { await prepareFileURLs(vm); return plane; };
  const box = { provider: "arker", sandboxId: "vm", getInstance: () => vm, getUrl: async () => "https://sandbox.example.test" };
  setCompatComputeFactoryForTests((() => ({ sandbox: { create: async () => box } })) as never);
  const sandbox = await Sandbox.create();
  assert.equal(await sandbox.commands.run("echo ready"), "echo ready");
  await assert.rejects(new E2BFileURLs(vm, undefined, descriptor).downloadUrl("file"), actual => actual === error);
  assert.equal(calls.length, 1);
});
