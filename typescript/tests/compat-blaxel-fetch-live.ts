import assert from "node:assert/strict";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SandboxInstance as Native, settings } from "@blaxel/core";
import { SandboxInstance as Shim } from "../src/blaxel.js";
import { recordEvidence } from "../scripts/compat-evidence.js";
import { traceVendor } from "./compat-trace.js";
import { Arker } from "../src/index.js";

const source = await readFile(
  new URL("../../compatibility/http-service.py", import.meta.url),
  "utf8",
);

async function program(
  SandboxInstance: typeof Native | typeof Shim,
  configure: (sandbox: Native | Shim) => Promise<void>,
): Promise<void> {
  const sandbox = await SandboxInstance.create({
    name: "arker-fetch-" + crypto.randomUUID().slice(0, 12),
  });
  console.log(
    "FETCH created",
    sandbox.metadata.name,
    settings.disableH2 ? "fallback" : "h2",
  );
  try {
    await configure(sandbox);
    const checks = async () => {
      await sandbox.fs.write("/tmp/compat-fetch-service.py", source);
      await sandbox.process.exec({
        command: "python3 /tmp/compat-fetch-service.py",
        name: "fetch-service",
        waitForCompletion: false,
      });
      const deadline = Date.now() + 30000;
      while (
        !(await sandbox.process.get("fetch-service")).stdout.includes(
          "fetch-service-ready",
        )
      ) {
        assert.ok(Date.now() < deadline, "HTTP origin did not become ready");
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      const body = Uint8Array.from(
        { length: 262144 },
        (_, index) => index % 256,
      );
      console.log("FETCH binary POST", sandbox.metadata.name);
      const response = await sandbox.fetch(18082, "echo?x=1", {
        method: "POST",
        body,
        headers: [
          ["authorization", "Bearer application-only"],
          ["x-api-key", "earlier-value"],
          ["x-api-key", "application-key"],
        ],
      });
      assert.equal(response.status, 207);
      assert.equal(
        response.headers.get("x-echo-authorization"),
        "Bearer application-only",
      );
      assert.equal(response.headers.get("x-echo-key"), "application-key");
      assert.deepEqual(new Uint8Array(await response.arrayBuffer()), body);
      const chunked = await sandbox.network.fetch(18082, "/chunked");
      assert.equal(chunked.status, 418);
      assert.deepEqual(
        new Uint8Array(await chunked.arrayBuffer()),
        Uint8Array.from([101, 111, 102, 0, 255, 1]),
      );
      const redirect = await sandbox.fetch(18082, "/redirect", {
        redirect: "manual",
      });
      assert.equal(redirect.status, 302);
      assert.equal(redirect.headers.get("location"), "chunked");
      await redirect.body?.cancel();
      const head = await sandbox.fetch(18082, "/head", { method: "HEAD" });
      assert.equal(head.status, 200);
      if (settings.disableH2) assert.equal(head.body, null);
      else assert.notEqual(head.body, null);
      console.log("FETCH HEAD body", sandbox.metadata.name);
      assert.equal((await head.arrayBuffer()).byteLength, 0);
      const controller = new AbortController();
      console.log("FETCH quiet headers", sandbox.metadata.name);
      const quiet = await sandbox.fetch(18082, "/quiet", {
        signal: controller.signal,
      });
      const reader = quiet.body!.getReader();
      assert.deepEqual(
        new Uint8Array((await reader.read()).value!),
        Uint8Array.of(120),
      );
      const pending = reader.read();
      controller.abort();
      console.log("FETCH aborted body", sandbox.metadata.name);
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const bounded = Promise.race([
          pending,
          new Promise<never>((_, reject) => {
            timer = setTimeout(
              () => reject(new Error("Aborted response body remained pending")),
              15000,
            );
          }),
        ]);
        await assert.rejects(bounded, { name: "AbortError" });
      } finally {
        clearTimeout(timer);
        await reader.cancel().catch((error) => {
          if (error?.name !== "AbortError") throw error;
        });
      }
    };
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        checks(),
        new Promise<never>((_, reject) => {
          timeout = setTimeout(
            () =>
              reject(
                new Error("Private-fetch scenario exceeded its test deadline"),
              ),
            60000,
          );
        }),
      ]);
    } finally {
      clearTimeout(timeout);
    }
  } finally {
    await sandbox.delete();
  }
}

if (!process.env.ARKER_API_KEY || !process.env.BL_API_KEY)
  throw new Error("Live credentials required");
assert(process.env.BL_REGION, "BL_REGION is required to exercise Blaxel H2 rather than its regionless Fetch fallback");
const directory = await mkdtemp(join(tmpdir(), "compat-private-fetch-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
const previousH2 = settings.config.disableH2;
try {
  for (const disableH2 of [false, true]) {
    settings.config.disableH2 = disableH2;
    for (const [provider, constructor] of [
      ["blaxel", Native],
      ["blaxel", Shim],
      ["arker", Shim],
    ] as const) {
      const config = join(directory, "router.json");
      await writeFile(
        config,
        JSON.stringify({
          version: 1,
          existing: { strategy: "priority", order: [provider] },
          create: { strategy: "priority", order: [provider] },
        }),
      );
      process.env.COMPUTESDK_ROUTER_CONFIG = config;
      const called = new Set<string>();
      let outcome = "failed";
      try {
        await program(
          traceVendor(constructor, "SandboxInstance.static", called),
          async (sandbox) => {
            if (provider === "blaxel") {
              assert.equal(Boolean(sandbox.h2Domain), !disableH2,
                "Native sandbox did not select the requested H2/Fetch profile");
            }
            assert.equal(
              settings.disableH2,
              disableH2,
              "Creation changed the requested HTTP transport profile",
            );
            if (provider === "arker") {
              await new Arker().vm((sandbox as Shim).id).setPolicies({
                policies: [
                  { type: "inbound", action: "deny" },
                  { type: "outbound", action: "deny" },
                ],
              });
            }
          },
        );
        assert.equal(
          settings.disableH2,
          disableH2,
          "Operations changed the requested HTTP transport profile",
        );
        outcome = "passed";
      } finally {
        await recordEvidence(
          "blaxel",
          constructor === Native
            ? "reference"
            : provider === "arker"
              ? "arker"
              : "native",
          "import-only-private-fetch-" + (disableH2 ? "fallback" : "h2"),
          [...called].sort(),
          outcome,
        );
      }
      console.log(
        "PASS Blaxel private fetch: " +
          (constructor === Native ? "original" : "shim") +
          " on " +
          provider,
      );
    }
  }
} finally {
  settings.config.disableH2 = previousH2;
  if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
  else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
  await rm(directory, { recursive: true });
}
