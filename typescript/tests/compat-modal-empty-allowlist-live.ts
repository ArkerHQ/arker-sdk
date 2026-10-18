import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModalClient as Native, type Sandbox as NativeSandbox, type ContainerProcess } from "modal";
import { ModalClient as Shim, type Sandbox as ShimSandbox } from "../src/modal.js";
import { Arker } from "../src/index.js";

// Native app/image setup is not an Arker translation. No API coverage credit.
const connect = `import socket
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=3)
except OSError:
    print('blocked')
else:
    connection.close()
    print('connected')
`;

if (!process.env.ARKER_API_KEY || !process.env.MODAL_TOKEN_ID || !process.env.MODAL_TOKEN_SECRET)
  throw new Error("Live Arker and Modal credentials required");
const directory = await mkdtemp(join(tmpdir(), "compat-modal-egress-"));
const previous = process.env.COMPUTESDK_ROUTER_CONFIG;
const native = new Native();
let parent: Awaited<ReturnType<Arker["fork"]>> | undefined;
try {
  const app = await native.apps.fromName("arker-compat-image-test", { createIfMissing: true });
  const image = await native.images.fromRegistry("python:3.12-slim").build(app);
  parent = await new Arker().fork({ image: "python:3.12-slim" });
  for (const route of ["reference", "native", "arker"] as const) {
    const provider = route === "arker" ? "arker" : "modal";
    const path = join(directory, "router.json");
    await writeFile(path, JSON.stringify({
      version: 1,
      existing: { strategy: "priority", order: [provider] },
      create: { strategy: "priority", order: [provider] },
    }));
    process.env.COMPUTESDK_ROUTER_CONFIG = path;
    const shim: Shim = new Shim({ arker: { sourceVmId: parent.id } });
    try {
      for (const option of [undefined, "outboundCidrAllowlist", "outboundDomainAllowlist"] as const) {
        const options = option ? { [option]: [] } : {};
        const sandbox: NativeSandbox | ShimSandbox = route === "reference"
          ? await native.sandboxes.create(app, image, { timeoutMs: 90_000, ...options })
          : route === "native"
            ? await shim.sandboxes.create(app, image, { timeoutMs: 90_000, ...options })
            : await shim.sandboxes.create(options);
        try {
          const child: ContainerProcess<string> = await sandbox.exec(["python3", "-c", connect]);
          const output = await child.stdout.readText();
          assert.equal(await child.wait(), 0);
          assert.equal(output, option ? "blocked\n" : "connected\n", `${route}: ${option ?? "unrestricted"}`);
          console.log(`PASS Modal egress: ${route}, ${option ?? "unrestricted"}`);
        } finally {
          await sandbox.terminate();
        }
      }
    } finally {
      shim.close();
    }
  }
} finally {
  try {
    if (parent) await parent.delete();
  } finally {
    native.close();
    if (previous === undefined) delete process.env.COMPUTESDK_ROUTER_CONFIG;
    else process.env.COMPUTESDK_ROUTER_CONFIG = previous;
    await rm(directory, { recursive: true, force: true });
  }
}
