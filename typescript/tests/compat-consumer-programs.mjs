export const programs = {
  base: `import { Arker } from "@arker-ai/sdk";
async function check() { const vm = await new Arker().fork({ image: "ubuntu:24.04" }); const id: string = vm.id; return id; }
void check;`,
  e2b: `import { Sandbox } from "__IMPORT__";
import type { CommandHandle } from "e2b";
async function check() {
  const sandbox = await Sandbox.create();
  const process: CommandHandle = await sandbox.commands.run("cat", { background: true, stdin: true });
  await process.sendStdin("input"); await process.closeStdin();
  const result = await process.wait(); const code: number = result.exitCode;
  await Sandbox.kill(sandbox.sandboxId); return code;
}
void check;`,
  blaxel: `import { SandboxInstance } from "__IMPORT__";
async function check() {
  const sandbox = await SandboxInstance.create({ name: "consumer-types" });
  const result = await sandbox.process.exec({ command: "true", waitForCompletion: true });
  const output: string | undefined = result.stdout;
  await sandbox.fs.write("/tmp/file", "content");
  await sandbox.fs.readBinary("/tmp/file"); await sandbox.delete(); return output;
}
void check;`,
  daytona: `import { Daytona, Image } from "__IMPORT__";
async function check() {
  const client = new Daytona({ apiKey: "test", apiUrl: "https://daytona.invalid/api", target: "us" });
  try {
    const sandbox = await client.create({ image: Image.base("alpine:3.21") });
    const response = await sandbox.process.executeCommand("true", "/tmp", { VALUE: "kept" }, 7);
    const output: string = response.artifacts!.stdout; await client.delete(sandbox); return output;
  } finally { await client[Symbol.asyncDispose](); }
}
void check;`,
  modal: `import { ModalClient } from "__IMPORT__";
import type { ContainerProcess } from "modal";
async function check() {
  const client = new ModalClient({ tokenId: "test", tokenSecret: "test" });
  try {
    const app = await client.apps.fromName("consumer", { createIfMissing: true });
    const image = await client.images.fromRegistry("python:3.12-slim").build(app);
    const sandbox = await client.sandboxes.create(app, image, { blockNetwork: true, encryptedPorts: [8443] });
    const process: ContainerProcess<string> = await sandbox.exec(["cat"]);
    await process.stdin.writeText("input"); await process.stdin.close();
    const output: string = await process.stdout.readText();
    const code: number = await sandbox.terminate({ wait: true }); return { output, code };
  } finally { client.close(); }
}
void check;`,
};
