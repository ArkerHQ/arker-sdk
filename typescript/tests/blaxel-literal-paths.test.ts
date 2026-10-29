import assert from "node:assert/strict";
import { test } from "node:test";
import { SandboxInstance } from "../src/blaxel.js";

test("Blaxel generated filesystem paths preserve literal tilde prefixes", async () => {
  const paths: string[] = [];
  const commands: string[] = [];
  const box = {
    provider: "arker", sandboxId: "vm-test",
    getInstance: () => ({ sync: async (path: string) => { paths.push(path); return new Uint8Array([1]); } }),
    filesystem: {
      readFile: async (path: string) => { paths.push(path); return "text"; },
      writeFile: async (path: string) => { paths.push(path); },
      readdir: async (path: string) => { paths.push(path); return []; },
    },
    runCommand: async (command: string) => { commands.push(command); return { exitCode: 0, stdout: "file", stderr: "" }; },
  };
  const fs = (Reflect.construct(SandboxInstance, [box]) as SandboxInstance).fs;
  for (const path of ["~", "~/a", "~root/a", "~//a", "~/../a"])
    await fs.readBinary(path);
  assert.deepEqual(paths.splice(0), ["/~", "/~/a", "/~root/a", "/~/a", "/a"]);
  assert.equal(await fs.read("~/a"), "text");
  assert.equal((await fs.write("~/b", "text")).path, "/~/b");
  assert.equal((await fs.ls("~/dir")).path, "/~/dir");
  await fs.writeTree([{ path: "a", content: "tree" }], "~/tree");
  assert.deepEqual(paths, ["/~/a", "/~/b", "/~/dir", "/~/tree/a", "/~/tree"]);
  await fs.mkdir("~/dir");
  await fs.rm("~/a");
  assert(commands.every(command => command.includes("/~")));
});
