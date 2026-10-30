import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { InvalidArgumentError, TemplateError } from "e2b";
import { createE2BDataPlane, scopedEnvdFetch } from "../src/compat/e2b-data-plane.js";
import { listen, closeServer } from "./provider-fixtures.js";
import { serveDataPlane } from "./e2b-http-fixtures.js";

function frame(value: unknown, flags = 0) {
  const payload = Buffer.from(JSON.stringify(value)), header = Buffer.alloc(5);
  header[0] = flags; header.writeUInt32BE(payload.length, 1);
  return Buffer.concat([header, payload]);
}

for (const retainOutput of [false, true]) for (const operation of ["run", "background", "connect"] as const) {
  test(`${operation} retainOutput=${retainOutput} preserves callbacks`, async t => {
    let calls = 0;
    const server = createServer((req, res) => {
      calls++;
      assert.equal(req.headers["x-api-key"], "platform");
      assert.equal(req.headers["x-access-token"], "guest");
      req.resume(); res.writeHead(200, { "content-type": "application/connect+json" });
      res.write(frame({ event: { start: { pid: 123 } } }));
      for (const [name, value] of [["stdout", "hello "], ["stdout", "世界"], ["stderr", "warning"]]) {
        res.write(frame({ event: { data: { [name]: Buffer.from(value!).toString("base64") } } }));
      }
      res.write(frame({ event: { end: { exitCode: 0, exited: true, status: "exited" } } }));
      res.end(frame({}, 2));
    });
    const data = await serveDataPlane(t, server);
    const stdout: string[] = [], stderr: string[] = [];
    const opts = { retainOutput, onStdout: (s: string) => { stdout.push(s); }, onStderr: (s: string) => { stderr.push(s); } };
    const result = operation === "run" ? await data.commands.run("printf output", opts)
      : await (operation === "background"
        ? await data.commands.run("printf output", { ...opts, background: true })
        : await data.commands.connect(123, opts)).wait();
    assert.equal(stdout.join(""), "hello 世界"); assert.equal(stderr.join(""), "warning");
    assert.equal(result.stdout, retainOutput ? "hello 世界" : undefined);
    assert.equal(result.stderr, retainOutput ? "warning" : undefined);
    assert.equal(result.exitCode, 0); assert.equal(calls, 1);
  });
}

for (const kind of ["iterable", "readable"] as const) {
  test(`${kind} upload streams before the producer finishes`, async t => {
    let arrived!: () => void;
    const arrival = new Promise<void>(resolve => { arrived = resolve; });
    const received: Buffer[] = [];
    const server = createServer(async (req, res) => {
      assert.equal(req.headers["content-type"], "application/octet-stream");
      assert.equal(req.headers["x-api-key"], "platform");
      assert.equal(req.headers["x-access-token"], "guest");
      arrived();
      for await (const chunk of req) received.push(Buffer.from(chunk));
      res.writeHead(200, { "content-type": "application/json" });
      res.end('[{"name":"file","path":"/tmp/file","type":"file"}]');
    });
    const data = await serveDataPlane(t, server);
    const chunks = [Buffer.from("first\0"), Buffer.alloc(0), Buffer.from("世界"), Buffer.from("last")];
    async function* source() {
      yield chunks[0]!;
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([arrival, new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("upload buffered before request dispatch")), 5000);
        })]);
      } finally { clearTimeout(timer); }
      yield* chunks.slice(1);
    }
    const iterator = source();
    const input = kind === "iterable" ? iterator : new ReadableStream<Uint8Array>({
      async pull(controller) {
        const next = await iterator.next();
        if (next.done) controller.close(); else controller.enqueue(next.value);
      },
      async cancel() { await iterator.return(); },
    });
    const result = await data.files.write("/tmp/file", input);
    assert.equal(result.path, "/tmp/file");
    assert.deepEqual(Buffer.concat(received), Buffer.concat(chunks));
  });
}

test("iterable multipart rejection happens before dispatch or consumption", async t => {
  let calls = 0, consumed = false;
  const data = await serveDataPlane(t, createServer((req, res) => {
    calls++; req.resume(); res.end("[]");
  }));
  async function* source() { consumed = true; yield new Uint8Array([1]); }
  await assert.rejects(data.files.write("/tmp/file", source(), { useOctetStream: false }),
    error => error instanceof InvalidArgumentError);
  assert.equal(calls, 0); assert.equal(consumed, false);
});

test("old envd refuses iterable upload before dispatch or consumption", async t => {
  let calls = 0, consumed = false;
  const server = createServer((req, res) => { calls++; req.resume(); res.end("[]"); });
  const address = await listen(server);
  t.after(() => closeServer(server));
  const data = await createE2BDataPlane({ endpoint: `http://127.0.0.1:${address.port}`,
    envd_version: "0.5.6", envd_access_token: "guest" }, "platform");
  t.after(() => data.close());
  async function* source() { consumed = true; yield new Uint8Array([1]); }
  await assert.rejects(data.files.write("/tmp/file", source()), error => error instanceof TemplateError);
  assert.equal(calls, 0); assert.equal(consumed, false);
});

test("aborting an iterable upload closes its producer and request", { timeout: 5000 }, async t => {
  let arrived!: () => void, disconnected!: () => void, release!: () => void, finished!: () => void;
  const arrival = new Promise<void>(resolve => { arrived = resolve; });
  const disconnect = new Promise<void>(resolve => { disconnected = resolve; });
  const next = new Promise<void>(resolve => { release = resolve; });
  const final = new Promise<void>(resolve => { finished = resolve; });
  const data = await serveDataPlane(t, createServer((req, res) => {
    res.on("close", disconnected); req.resume(); arrived();
  }));
  const controller = new AbortController();
  async function* source() {
    try { yield new Uint8Array([1]); await next; yield new Uint8Array([2]); }
    finally { finished(); }
  }
  const pending = data.files.write("/tmp/file", source(), { signal: controller.signal });
  const rejected = assert.rejects(pending);
  try {
    await arrival; controller.abort(); release();
    await rejected; await final; await disconnect;
  } finally { release(); }
});

// Some fetch implementations reject on abort without cancelling their body reader.
test("scoped fetch owns upload cancellation independently of fetch", async () => {
  let cancelled = false, arrived!: () => void;
  const arrival = new Promise<void>(resolve => { arrived = resolve; });
  const controller = new AbortController();
  const body = new ReadableStream<Uint8Array>({
    start(stream) { stream.enqueue(new Uint8Array([1])); stream.enqueue(new Uint8Array([2])); },
    cancel() { cancelled = true; },
  });
  const scoped = scopedEnvdFetch({ endpoint: "https://envd.test", envd_version: "0.10.2", envd_access_token: "guest" },
    "platform", async input => {
      assert.ok(input instanceof Request);
      await input.body!.getReader().read();
      arrived();
      return await new Promise<Response>((_resolve, reject) => {
        input.signal.addEventListener("abort", () => reject(input.signal.reason), { once: true });
      });
    });
  const pending = scoped("https://envd.test/files", { method: "POST", body, signal: controller.signal });
  const rejected = assert.rejects(pending);
  await arrival; controller.abort(); await rejected;
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});

for (const status of [204, 400]) for (const cleanupFails of [false, true]) {
  test(`early ${status} response finalizes upload (cleanup rejects=${cleanupFails})`, async () => {
    let cancelled = 0;
    let signal: AbortSignal | undefined;
    const upload = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array([1])); },
      cancel() {
        cancelled++;
        if (cleanupFails) throw new Error("producer cleanup failed");
      },
    });
    const scoped = scopedEnvdFetch({ endpoint: "https://envd.test", envd_version: "0.10.2", envd_access_token: "guest" },
      "platform", async input => {
        assert.ok(input instanceof Request); signal = input.signal;
        return new Response(status === 204 ? null : "native failure body", { status });
      });
    const response = await scoped("https://envd.test/files", { method: "POST", body: upload });
    assert.equal(response.status, status);
    assert.equal(signal?.aborted, false);
    assert.equal(await response.text(), status === 204 ? "" : "native failure body");
    // Cancellation settlement releases the reader without replacing the response.
    await Promise.resolve();
    assert.equal(cancelled, 1);
    assert.equal(upload.locked, false);
    assert.equal(signal?.aborted, false);
  });
}

test("response read failure preserves its error while finalizing an unfinished upload", async () => {
  let cancelled = 0;
  const primary = new Error("response interrupted");
  const upload = new ReadableStream<Uint8Array>({ cancel() { cancelled++; throw new Error("cleanup failed"); } });
  const scoped = scopedEnvdFetch({ endpoint: "https://envd.test", envd_version: "0.10.2", envd_access_token: "guest" },
    "platform", async () => new Response(new ReadableStream({ start(controller) { controller.error(primary); } }), { status: 400 }));
  const response = await scoped("https://envd.test/files", { method: "POST", body: upload });
  await assert.rejects(response.text(), error => error === primary);
  await Promise.resolve();
  assert.equal(cancelled, 1); assert.equal(upload.locked, false);
});
