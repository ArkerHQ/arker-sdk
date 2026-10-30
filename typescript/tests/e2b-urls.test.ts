import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";
import { Sandbox as NativeSandbox } from "e2b";
import { E2BFileURLs, prepareFileURLs } from "../src/compat/e2b-urls.js";
import type { VM } from "../src/index.js";

const token = "test-envd-token-not-a-platform-key";
async function fixture(response = delegationResponse()) {
  const calls: unknown[][] = [];
  const vm = { id: "name /?", baseUrl: "https://example.test/api", _client: {
    _request: async (...args: unknown[]) => { calls.push(args); return response; },
  } } as unknown as VM;
  await prepareFileURLs(vm);
  calls.length = 0;
  return { vm, urls: new E2BFileURLs(vm, undefined, { endpoint: "https://sandbox.example.test", envd_version: "0.10.2", envd_access_token: token }), calls };
}

test("native secured signatures preserve optional upload path, custom users, literal paths and expiry", async () => {
  const native = new (NativeSandbox as any)({ sandboxId: "test", apiKey: "test-key", envdVersion: "0.10.2", envdAccessToken: token });
  const now = Date.now; Date.now = () => 1800000000000;
  try {
    for (const path of [undefined, "", "../a", "~/a", "~other/a", "link/../a", "a?username=root&path=/other"]) {
      for (const user of [undefined, "root", "image-user"]) {
        for (const expiry of [undefined, 0, 86401, 4294967295]) {
          const options = { user, useSignatureExpiration: expiry };
          const expected = new URL(await native.uploadUrl(path, options));
          const { urls, calls } = await fixture();
          const url = new URL(await urls.uploadUrl(path, options));
          const body = verifyURL(url.toString());
          assert.equal(body.path, path ?? "");
          assert.equal(body.user, user);
          assert.equal(body.envd_signature, expected.searchParams.get("signature"));
          assert.equal(body.expires_at, expiry === undefined ? undefined : 1800000000 + expiry);
          assert.equal(JSON.stringify(calls).includes(token), false);
          assert.equal(calls.length, 0);
        }
      }
    }
  } finally { Date.now = now; }
});

test("invalid scope fails before mint and bootstrap is required", async () => {
  const { urls, calls } = await fixture();
  for (const expiry of [-1, Infinity, 0.1, 4294967296]) await assert.rejects(urls.downloadUrl("a", { useSignatureExpiration: expiry }));
  await assert.rejects(urls.downloadUrl(""));
  await assert.rejects(urls.downloadUrl("a", { user: "bad:user" }));
  await assert.rejects(new E2BFileURLs().uploadUrl(), /initialized/);
  assert.equal(calls.length, 0);
});

test("native provider dispatch keeps optional path", async () => {
  const urls = new E2BFileURLs(undefined, { uploadUrl: async (path) => { assert.equal(path, undefined); return "native"; }, downloadUrl: async () => "native" });
  assert.equal(await urls.uploadUrl(), "native");
});


function delegationResponse(updates = {}) {
  return { redemption_url: "https://regional.example.test/api/v1/vms/canonical/file-transfer",
    context: "b3BhcXVl", signing_key: Buffer.from(Array.from({ length: 32 }, (_, i) => i)).toString("base64url"),
    operations: ["read", "write"], expires_at: null, ...updates };
}
function verifyURL(value: string): Record<string, any> {
  const url = new URL(value);
  assert.equal(url.host, "regional.example.test");
  const [context, payload, tag] = url.searchParams.get("ticket")!.replace(/^ark_file_v1_/, "").split(".");
  assert.equal(context, "b3BhcXVl");
  const expected = createHmac("sha256", Buffer.from(Array.from({ length: 32 }, (_, i) => i))).update("arker:file-transfer:v1\0").update(`${context}.${payload}`).digest("base64url");
  assert.equal(tag, expected);
  return JSON.parse(Buffer.from(payload, "base64url").toString());
}

test("preparation is the only network call and authority never appears in URLs or VM serialization", async () => {
  const { vm, urls, calls } = await fixture();
  await prepareFileURLs(vm);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].slice(0, 3), ["POST", "/v1/vms/name%20%2F%3F/file-transfer-delegation", {}]);
  calls.length = 0;
  verifyURL(await urls.downloadUrl("a"));
  verifyURL(await urls.uploadUrl("b"));
  assert.equal(calls.length, 0);
  assert.equal(JSON.stringify(vm).includes(delegationResponse().signing_key), false);
  const fresh = { ...vm };
  await assert.rejects(new E2BFileURLs(fresh, undefined, { endpoint: "https://sandbox.example.test", envd_version: "0.10.2", envd_access_token: token }).downloadUrl("a"), /prepared/);
  assert.equal(calls.length, 0);
});

test("invalid delegated URL, key, grants and cap are rejected", async () => {
  for (const updates of [
    { redemption_url: "http://regional.example.test/files" },
    { redemption_url: "https://user:password@regional.example.test/files" },
    { redemption_url: "https://regional.example.test/files?" },
    { redemption_url: "https://regional.example.test/files#" },
    { context: "abc=" }, { signing_key: "a" }, { operations: [] },
    { operations: ["read", "read"] }, { expires_at: -1 },
  ]) await assert.rejects(fixture(delegationResponse(updates)));
});

test("read-only delegation and finite parent lifetime constrain local URLs", async () => {
  const now = Date.now; Date.now = () => 100000;
  try {
    const { urls, calls } = await fixture(delegationResponse({ operations: ["read"], expires_at: 105 }));
    assert.equal(verifyURL(await urls.downloadUrl("a", { useSignatureExpiration: 5 })).expires_at, 105);
    await assert.rejects(urls.downloadUrl("a"), /lifetime/);
    await assert.rejects(urls.downloadUrl("a", { useSignatureExpiration: 6 }), /lifetime/);
    await assert.rejects(urls.uploadUrl("a", { useSignatureExpiration: 1 }), /operation/);
    Date.now = () => 106000;
    await assert.rejects(urls.downloadUrl("a", { useSignatureExpiration: 0 }), /lifetime/);
    assert.equal(calls.length, 0);
  } finally { Date.now = now; }
});

function facadeVM(failDelegation = false) {
  const calls: string[] = [];
  const root = "/root/.arker/e2b-envd";
  const lifetime = { incarnation: { vm_id: "owned", pid: 10, start: "20", boot_id: "boot" }, session_id: "hold" };
  const vm: any = {
    id: "owned", baseUrl: "https://api.example.test",
    _client: {
      _authHeaders: () => ({ authorization: "Bearer synthetic-platform-key" }),
      _request: async (method: string, path: string, body: unknown) => {
        assert.deepEqual([method, path, body], ["POST", "/v1/vms/owned/file-transfer-delegation", {}]);
        calls.push("delegate");
        if (failDelegation) throw new Error("delegation unavailable");
        return delegationResponse();
      },
    },
    refresh: async () => ({ hostname: "owned.example.test" }),
    sync: async (_path: string, data?: unknown) => data === undefined ? Buffer.from(JSON.stringify({ vm_id: "owned", token: "a".repeat(43) })) : undefined,
    getRun: async (runId: string) => {
      assert.equal(runId, "monitor-run");
      calls.push("ready");
      return { state: "running", vm_id: "owned", session_id: "hold",
        stdoutBytes: Buffer.from(JSON.stringify({ ready: true, lifetime }) + "\n") };
    },
    run: async (command: string, options?: any) => {
      if (command.includes(" lifetime ")) {
        calls.push("inspect");
        return { exitCode: 0, stdout: JSON.stringify(lifetime) };
      }
      if (command.includes(" monitor ")) {
        assert.deepEqual(options, {
          session_id: "hold", timeout: 0, time_to_background: 0,
          end_after_all_subprocesses: true, idempotencyKey: options.idempotencyKey,
        });
        assert.match(options.idempotencyKey, /^[a-f0-9]{64}$/);
        calls.push("admit");
        return { type: "background", runId: "monitor-run" };
      }
      if (command.includes(" publish ")) {
        assert.deepEqual(calls, ["inspect", "admit", "ready"]);
        calls.push("publish");
        return { exitCode: 0, stdout: JSON.stringify({ ready: true }) };
      }
      if (command.includes(" prepare ")) return { exitCode: 0, stdout: JSON.stringify({ root_path: root, request_directory: root + "/requests", state_path: root + "/state.json", existing_state_path: root + "/state.json", arch: "amd64" }) };
      if (command.includes(" verify ")) return { exitCode: 0, stdout: JSON.stringify({ verified: true, arch: "amd64" }) };
      if (command.includes(" ensure ")) return { exitCode: 0, stdout: JSON.stringify({ ready: true, port: 49983, version: "0.10.2" }) };
      throw new Error("unexpected guest command");
    },
    delete: async () => { calls.push("delete"); },
  };
  const box = { provider: "arker", sandboxId: "owned", getInstance: () => vm, getUrl: async () => "https://owned.example.test" };
  return { box, calls };
}

test("facade preparation retains local URL authority and failure cleans up owned VM", async () => {
  const { Sandbox } = await import("../src/e2b.js");
  const { box, calls } = facadeVM();
  const plane = await (Sandbox as any).dataPlane(box);
  try {
    const sandbox = new (Sandbox as any)(box, undefined, undefined, plane);
    verifyURL(await sandbox.downloadUrl("~/file"));
    verifyURL(await sandbox.uploadUrl());
    assert.deepEqual(calls, ["inspect", "admit", "ready", "publish", "delegate"]);
  } finally { plane.close(); }
  const failed = facadeVM(true);
  await assert.rejects((Sandbox as any).wrap(failed.box, { deleteOnValidationFailure: true }), /delegation unavailable/);
  assert.deepEqual(failed.calls, ["inspect", "admit", "ready", "publish", "delegate", "delete"]);
});

test("shared wire vector matches Rust verifier fixture", async () => {
  const { readFileSync } = await import("node:fs");
  const vector = JSON.parse(readFileSync("../compatibility/e2b-file-delegation-vector.json", "utf8"));
  const { vm } = await fixture(vector.response);
  const now = Date.now; Date.now = () => vector.now * 1000;
  try {
    const urls = new E2BFileURLs(vm, undefined, { endpoint: "https://guest.example.test", envd_version: "0.10.2", envd_access_token: vector.envd_token });
    const url = await urls.downloadUrl(vector.path, { user: vector.user, useSignatureExpiration: vector.expiry });
    assert.equal(new URL(url).searchParams.get("ticket"), vector.ticket);
  } finally { Date.now = now; }
});
