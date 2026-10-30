import assert from "node:assert/strict";
import { test } from "node:test";
import { GuestProcesses } from "../src/compat/guest-process.js";
import type { SandboxInterface } from "computesdk";

test("large stdin writes stay bounded and serialize whole writes before EOF", async () => {
  const manager = new GuestProcesses({} as SandboxInterface);
  const calls: Array<{ action: string; data: Buffer }> = [];
  (manager as any).callOnce = async (
    action: string,
    values: { data?: string },
  ) => {
    const data = values.data
      ? Buffer.from(values.data, "base64")
      : Buffer.alloc(0);
    assert.ok(data.length <= 32768);
    calls.push({ action, data });
    await Promise.resolve();
    return { message: "updated" };
  };
  const first = Buffer.alloc(70000, 1);
  const second = Buffer.alloc(70000, 2);
  await Promise.all([
    manager.writeStdin("42", first),
    manager.writeStdin("42", second),
    manager.closeStdin("42"),
  ]);
  assert.equal(calls.at(-1)?.action, "closeStdin");
  assert.deepEqual(
    Buffer.concat(calls.map((call) => call.data)),
    Buffer.concat([first, second]),
  );
  assert.equal(calls.filter((call) => call.action === "writeStdin").length, 6);
});
