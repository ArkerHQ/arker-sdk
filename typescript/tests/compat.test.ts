// What the live compatibility tests cannot reach: the default source, and
// handing ids Arker does not know to the original provider's package.
// @ts-expect-error bun:test ships with the runner; this package installs no bun types.
import { expect, mock, test } from "bun:test";
import { readFileSync } from "node:fs";

import { arkerSource, CompatSandbox, splitAddresses } from "../src/compat/core.js";
import { FILE_SERVER, INTERPRETER, NODE_INTERPRETER } from "../src/compat/interpreter.js";
import { VM } from "../src/index.js";
import { Daytona, DaytonaNotFoundError, DaytonaNotSupportedError } from "../src/daytona.js";
import { Sandbox as E2BSandbox, NotSupportedError, SandboxNotFoundError } from "../src/e2b.js";
import { ModalClient } from "../src/modal.js";

const BASE = "https://test.invalid/api";

type Reply = [method: string, path: string, status: number, body: unknown];

/** Serve the scripted replies in any order; record request bodies. */
function arker(...replies: Reply[]) {
  const bodies: unknown[] = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const method = init?.method ?? "GET";
    const index = replies.findIndex(([m, path]) => m === method && `${BASE}${path}` === String(input));
    if (index === -1) throw new Error(`unexpected ${method} ${input}`);
    const [, , status, body] = replies.splice(index, 1)[0]!;
    bodies.push(typeof init?.body === "string" ? JSON.parse(init.body) : undefined);
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  };
  return { config: { apiKey: "ark_live_test", baseUrl: BASE, fetch, retry: false as const }, bodies };
}

const notFound = (id: string): Reply => [
  "GET",
  `/v1/vms/${id}`,
  404,
  { error: { code: "not_found", message: "not found", timestamp: "2026-01-01T00:00:00Z" } },
];

const vm = (id: string) => ({
  vm_id: id,
  owner_org_id: "owner",
  created_at: "now",
  description: null,
  public: false,
  state: "idle",
  sessions: [],
  network: {},
  resources: {},
});

// A source VM with the rules this layer sets, which a new sandbox must not
// take, and one rule of its owner's own.
const SOURCE = {
  ...vm("ubuntu-coding"),
  policies: {
    rules: {
      "compat-port-8000": { type: "inbound", priority: 108000, match: { ports: [8000] }, action: "allow", auth: "open" },
      "compat-suspend": { type: "schedule", at: "2026-01-01T00:00:00Z", action: "suspend" },
      mine: { type: "outbound", priority: 5, action: "allow" },
    },
  },
};

test("the default source is ubuntu-coding", () => {
  expect(arkerSource({})).toBe(process.env.ARKER_SOURCE || process.env.ARKER_SOURCE_VM || "ubuntu-coding");
  expect(arkerSource({ source: "mine" })).toBe("mine");
});

test("create forks the default source with a timeout", async () => {
  const written: Record<string, unknown> = {};
  const sync = VM.prototype.sync;
  VM.prototype.sync = (async function (path: string, data?: unknown) {
    written[path] = data;
  }) as typeof sync;
  const api = arker(["GET", "/v1/vms/ubuntu-coding", 200, SOURCE], ["POST", "/v1/fork", 200, vm("vm_new")]);
  const sandbox = await E2BSandbox.create({ arker: { ...api.config, source: "ubuntu-coding" } }).finally(() => {
    VM.prototype.sync = sync;
  });
  expect(sandbox.sandboxId).toBe("vm_new");
  const fork = api.bodies[1] as { source_vm_name: string; policies: { rules: Record<string, unknown> } };
  expect(fork.source_vm_name).toBe("ubuntu-coding");
  expect(fork.policies.rules).toEqual({
    "compat-port-8000": null,
    "compat-suspend": null,
    "compat-timeout": { type: "schedule", after_created: 300, action: "delete" },
  });
  expect(String(written["/etc/arker-compat.env"])).toContain("export E2B_SANDBOX_ID=vm_new\n");
});

// The provider packages are optional peers, absent here until the next test mocks them.
test("unknown ids without the provider are not found", async () => {
  const api = arker(notFound("old"), notFound("old"));
  await expect(E2BSandbox.connect("old", { arker: api.config })).rejects.toBeInstanceOf(SandboxNotFoundError);
  await expect(new Daytona({ arker: api.config }).get("old")).rejects.toBeInstanceOf(DaytonaNotFoundError);
});

test("unknown ids go to the installed provider", async () => {
  mock.module("e2b", () => ({ Sandbox: { connect: async (id: string) => `e2b:${id}` } }));
  mock.module("modal", () => ({
    ModalClient: class {
      sandboxes = { fromId: async (id: string) => `modal:${id}` };
    },
  }));
  mock.module("@daytonaio/sdk", () => ({
    Daytona: class {
      async get(id: string) {
        return `daytona:${id}`;
      }
    },
  }));
  const api = arker(notFound("old"), notFound("old"), notFound("old"));
  expect(await E2BSandbox.connect("old", { arker: api.config })).toBe("e2b:old" as never);
  expect(await new ModalClient({ arker: api.config }).sandboxes.fromId("old")).toBe("modal:old" as never);
  expect(await new Daytona({ arker: api.config }).get("old")).toBe("daytona:old" as never);
});

test("what is missing is named, with a way forward", async () => {
  const api = arker();
  let message = "";
  try {
    void (new Daytona({ arker: api.config }) as unknown as { secret: unknown }).secret;
  } catch (error) {
    expect(error).toBeInstanceOf(DaytonaNotSupportedError);
    message = (error as Error).message;
  }
  expect(message.startsWith("Daytona.secret is not supported by Arker's Daytona compatibility layer yet.")).toBe(true);
  for (const part of ["`.arker`", "/issues"]) expect(message).toContain(part);

  await expect(E2BSandbox.create({ arker: api.config, mcp: {} } as never)).rejects.toThrow(
    /^Sandbox\.create\(\{ mcp \}\) is not supported/,
  );
  await expect(new Daytona({ arker: api.config }).create({ autoPauseInterval: 5 } as never)).rejects.toBeInstanceOf(
    DaytonaNotSupportedError,
  );
  expect(api.bodies).toEqual([]);
});

test("inert options warn once and are ignored", async () => {
  const warn = mock(() => undefined);
  const original = console.warn;
  console.warn = warn;
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const api = arker(["GET", "/v1/vms/ubuntu-coding", 500, { error: { code: "internal", message: "stop here", timestamp: "now" } }]);
      await expect(new Daytona({ arker: api.config }).create({ autoStopInterval: 5 })).rejects.toThrow(/stop here/);
    }
  } finally {
    console.warn = original;
  }
  expect(warn.mock.calls).toEqual([["Daytona.create({ autoStopInterval }) has no effect on Arker and is ignored."]]);
});

test("listing skips VMs seen on an earlier page", async () => {
  const own = { ...vm("vm1"), created_at: "2026-01-01T00:00:00Z", description: '{"layer":"e2b"}' };
  const other = { ...vm("vm2"), description: '{"layer":"modal"}' };
  const api = arker(
    ["GET", "/v1/vms?limit=1000", 200, { vms: [own, other], next_cursor: "c2" }],
    ["GET", "/v1/vms?cursor=c2&limit=1000", 200, { vms: [own], next_cursor: null }],
  );
  const listed = await E2BSandbox.list({ arker: { ...api.config, controlBaseUrl: BASE } }).nextItems();
  expect(listed.map((info) => info.sandboxId)).toEqual(["vm1"]);
});

test("the scripts run in sandboxes are the Python SDK's, unchanged", () => {
  const read = (name: string) => readFileSync(new URL(`../../python/src/arker/${name}`, import.meta.url), "utf8");
  expect(INTERPRETER).toBe(read("_interpreter.py"));
  expect(NODE_INTERPRETER).toBe(read("_interpreter.js"));
  expect(FILE_SERVER).toBe(read("_file_server.py"));
});

test("sandbox envs written by either SDK read back whole, multi-line values too", async () => {
  const pem = "-----BEGIN KEY-----\nab'c \"d\" $e\n-----END KEY-----\n";
  // As the Python SDK writes them, with shlex.quote.
  let file = `export PEM='-----BEGIN KEY-----\nab'"'"'c "d" $e\n-----END KEY-----\n'\nexport EMPTY=''\nexport PLAIN=x\n`;
  const vm = Object.assign(Object.create(VM.prototype), {
    id: "vm",
    description: null,
    sync: async (_path: string, data?: string) => {
      if (data === undefined) return new TextEncoder().encode(file);
      file = data;
    },
  });
  const [other, failed] = [class extends Error {}, class extends Error {}];
  const errors = { provider: "Test", unsupported: other, invalid: other, notFound: other, fileNotFound: other, timeout: other, failed };
  const sandbox = new CompatSandbox(vm, errors);
  expect(await sandbox.env()).toEqual({ PEM: pem, EMPTY: "", PLAIN: "x" });
  await sandbox.updateEnv({ QUOTED: "it's" });
  expect(await sandbox.env()).toEqual({ PEM: pem, EMPTY: "", PLAIN: "x", QUOTED: "it's" });
  file = "export BROKEN='unclosed\n";
  await expect(sandbox.env()).rejects.toBeInstanceOf(failed);
});

test("addresses split into IPs and host names as the Python SDK splits them", () => {
  const entries = ["1.2.3.4", "10.0.0.0/8", "::1", "2001:db8::/32", "::ffff:1.2.3.4", "1.2.3.4/032"];
  const hosts = ["999.1.1.1", "host:8080", "1.2.3.4/33", "example.com", "01.2.3.4", "1.2.3", "*.example.com"];
  expect(splitAddresses([...entries, ...hosts])).toEqual([entries, hosts]);
});
