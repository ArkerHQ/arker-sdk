import assert from "node:assert/strict";
import { test } from "node:test";
import { Daytona as Native, Image, type Sandbox } from "@daytona/sdk";
import { createNativeDaytonaProvider } from "../src/compat/daytona-provider.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { preserveProperties } from "./provider-fixtures.js";

test("native Daytona bridge preserves Image objects, endpoint and target, errors and vendor instance", async (t) => {
  preserveProperties(t, Native.prototype, "create", "get");
  const image = Image.base("alpine:3.21").runCommands("echo marker");
  const box = { id: "native-image" } as Sandbox;
  const calls: unknown[] = [];
  Native.prototype.create = async function (params, options) {
    assert.equal((this as any).apiUrl, "https://daytona.invalid/api");
    assert.equal((this as any).target, "us");
    calls.push([params, options]);
    return box;
  };
  const provider = createNativeDaytonaProvider({
    apiKey: "dtn-test",
    apiUrl: "https://daytona.invalid/api",
    target: "us",
  });
  const sandbox = await provider.instances.create({
    image,
    envs: { VALUE: "kept" },
    name: "requested",
    timeout: 15500,
  } as any);
  assert.equal(sandbox.getInstance(), box);
  assert.deepEqual(calls, [
    [
      { image, envVars: { VALUE: "kept" }, name: "requested" },
      { timeout: 15.5 },
    ],
  ]);
  const denied = Object.assign(new Error("forbidden"), { statusCode: 403 });
  for (const [method, operation] of [
    ["get", () => provider.instances.getById("native-image")],
    ["create", () => provider.instances.create()],
  ] as const) {
    Native.prototype[method] = async () => {
      throw denied;
    };
    await assert.rejects(operation(), (error) => error === denied);
  }
  Native.prototype.get = async () => {
    throw Object.assign(new Error("missing"), { statusCode: 404 });
  };
  assert.equal(await provider.instances.getById("missing"), null);
});

test("ComputeSDK command normalization preserves exact output and exit status without changing native public execution", async (t) => {
  preserveProperties(t, Native.prototype, "create");
  const execute = promisify(execFile);
  Native.prototype.create = async () =>
    ({
      id: "native-command",
      process: {
        async executeCommand(
          command: string,
          cwd: string,
          env: Record<string, string>,
        ) {
          const { stdout } = await execute("/bin/sh", ["-c", command], {
            cwd,
            env: { ...process.env, ...env },
          });
          return { exitCode: 0, result: stdout };
        },
      },
    }) as unknown as Sandbox;
  const sandbox = await createNativeDaytonaProvider({
    apiKey: "dtn-test",
  }).instances.create();
  const result = await sandbox.runCommand(
    "printf '%s\\n' \"$VALUE\"; printf 'errλ\\n' >&2; exit 17",
    {
      cwd: "/tmp",
      env: { VALUE: "outλ" },
    },
  );
  assert.equal(result.stdout, "outλ\n");
  assert.equal(result.stderr, "errλ\n");
  assert.equal(result.exitCode, 17);
});
