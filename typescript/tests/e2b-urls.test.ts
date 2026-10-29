import assert from "node:assert/strict";
import { test } from "node:test";
import { Sandbox as NativeSandbox } from "e2b";
import { E2BFileURLs } from "../src/compat/e2b-urls.js";
import type { VM } from "../src/index.js";

const token = "test-envd-token-not-a-platform-key";
function fixture() {
  const calls: unknown[][] = [];
  const vm = { id: "name /?", baseUrl: "https://example.test/api", _client: {
    _request: async (...args: unknown[]) => { calls.push(args); return { vm_id: "canonical", ticket: "ark_file_v1_scoped.signature", expires_at: null }; },
  } } as unknown as VM;
  return { urls: new E2BFileURLs(vm, undefined, { endpoint: "https://sandbox.example.test", envd_version: "0.10.2", envd_access_token: token }), calls };
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
          const { urls, calls } = fixture();
          const url = new URL(await urls.uploadUrl(path, options));
          const body = calls[0][2] as Record<string, unknown>;
          assert.equal(body.path, path ?? "");
          assert.equal(body.user, user);
          assert.equal(body.envd_signature, expected.searchParams.get("signature"));
          assert.equal(body.expires_at, expiry === undefined ? undefined : 1800000000 + expiry);
          assert.equal(JSON.stringify(calls).includes(token), false);
          assert.deepEqual([...url.searchParams], [["ticket", "ark_file_v1_scoped.signature"]]);
        }
      }
    }
  } finally { Date.now = now; }
});

test("invalid scope fails before mint and bootstrap is required", async () => {
  const { urls, calls } = fixture();
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
