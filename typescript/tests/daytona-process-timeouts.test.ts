import assert from "node:assert/strict";
import { createServer, type RequestListener } from "node:http";
import { createRequire } from "node:module";
import { test } from "node:test";
import { Daytona as NativeDaytona, Process, DaytonaConnectionTimeoutError, DaytonaError, DaytonaInvalidArgumentError } from "@daytona/sdk";
import { Sandbox, Daytona } from "../src/daytona.js";
import { Arker } from "../src/index.js";
import { currentRequestTransport } from "../src/internal/request-context.js";
import { RequestIdleTimeoutError } from "../src/internal/idle-fetch.js";
import { executionRequestTimeout } from "../src/compat/daytona-request.js";

const require = createRequire(typeof __filename === "string" ? __filename : import.meta.url);
const vendorRequire = createRequire(require.resolve("@daytona/sdk"));
const { Configuration, ProcessApi } = vendorRequire("@daytona/toolbox-api-client");

async function serve(handler: RequestListener, run: (url: string) => Promise<void>) {
  const server = createServer(handler);
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  try { await run(`http://127.0.0.1:${(server.address() as {port: number}).port}`); }
  finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
}

function native(url: string, milliseconds: number) {
  const config = new Configuration({basePath: url});
  const client = (NativeDaytona as unknown as {createAxiosInstance(timeout: number): unknown}).createAxiosInstance(milliseconds);
  return new Process(config, new ProcessApi(config, "", client), async () => "", undefined, milliseconds);
}

function arker(url: string, milliseconds: number) {
  const client = new Arker({apiKey: "synthetic", baseUrl: url, retry: false, fetch});
  const box: any = {provider: "arker", sandboxId: "s", filesystem: {writeFile: async () => {}},
    runCommand: async () => {
      const value = await client._request<any>("GET", "/session/s");
      return {stdout: JSON.stringify({result: {session_id: value.sessionId, commands: []}}), stderr: "", exitCode: 0};
    }};
  return new Sandbox(box, undefined, milliseconds).process;
}

for (const route of [native, arker]) {
  test(`${route.name} metadata permits progress beyond configured timeout`, async () => {
    await serve((_request, response) => {
      const body = JSON.stringify({sessionId: "s", commands: []});
      response.writeHead(200, {"Content-Type": "application/json"});
      let offset = 0;
      response.write(body.slice(offset, offset += 8));
      const timer = setInterval(() => { response.write(body.slice(offset, offset += 8)); if (offset >= body.length) {clearInterval(timer); response.end();} }, 40);
      response.on("close", () => clearInterval(timer));
    }, async url => {
      const started = Date.now();
      assert.equal((await route(url, 100).getSession("s")).sessionId, "s");
      assert.ok(Date.now() - started >= 100);
    });
  });
  test(`${route.name} metadata silence times out before response`, async () => {
    let requests = 0;
    await serve(() => {requests++;}, async url => {
      await assert.rejects(route(url, 60).getSession("s"), DaytonaConnectionTimeoutError);
      assert.equal(requests, 1);
    });
  });
  test(`${route.name} body stall preserves native error category`, async () => {
    await serve((_request, response) => { response.writeHead(200); response.write('{"sessionId":'); }, async url => {
      await assert.rejects(route(url, 60).getSession("s"), error => error instanceof DaytonaError && !(error instanceof DaytonaConnectionTimeoutError));
    });
  });
  test(`${route.name} metadata zero disables configured timeout`, async () => {
    await serve((_request, response) => {const timer = setTimeout(() => response.end(JSON.stringify({sessionId: "s", commands: []})), 90); response.on("close", () => clearTimeout(timer));}, async url => {
      assert.equal((await route(url, 0).getSession("s")).sessionId, "s");
    });
  });
}

test("execution HTTP wait is independent of the server-side command deadline", async () => {
  assert.equal(executionRequestTimeout(50, 2), 7000);
  assert.equal(executionRequestTimeout(50, 0), 0);
  assert.equal(executionRequestTimeout(0, 2), 0);
  assert.equal(executionRequestTimeout(undefined, 2), undefined);
  const calls: any[] = [];
  const box: any = {provider: "arker", sandboxId: "s", filesystem: {}, runCommand: async (...args: any[]) => {
    calls.push({args, transport: currentRequestTransport()});
    return {stdout: "done", stderr: "", exitCode: 0};
  }};
  const process = new Sandbox(box, undefined, 50).process;
  await process.executeCommand("sleep 1", undefined, undefined, 2);
  assert.equal(calls[0].transport.idleTimeoutMs, 7000);
  assert.equal(calls[0].args[1].timeout, 2000);
  await process.executeCommand("sleep 1");
  assert.equal(calls[1].transport.idleTimeoutMs, 50);
  assert.equal(calls[1].args[1].timeout, undefined);
  assert.equal(currentRequestTransport(), undefined);
});

for (const value of [-1, Infinity, NaN]) test(`invalid config rejects before allocation ${value}`, () => {
  assert.throws(() => new Daytona({requestTimeoutMs: value}), DaytonaInvalidArgumentError);
});


test("session execution timeout stops waiting without sending a command kill", async () => {
  const operations: string[] = [];
  const box: any = {provider: "arker", sandboxId: "s", filesystem: {writeFile: async () => {}}, runCommand: async (command: string) => {
    const payload = JSON.parse(Buffer.from(command.split(" ").at(-1)!.replaceAll("'", ""), "base64").toString());
    operations.push(payload.action);
    return {stdout: JSON.stringify({result: {id: "running", command: "sleep 10", exit_code: null}}), stderr: "", exitCode: 0};
  }};
  const process = new Sandbox(box, undefined, 1000).process;
  await assert.rejects(process.executeSessionCommand("s", {command: "sleep 10"}, 0.05), DaytonaConnectionTimeoutError);
  assert.equal(operations[0], "execute");
  assert.ok(operations.slice(1).every(action => action === "command"));
});

test("caller cancellation is not changed into a request timeout", async () => {
  const {withRequestTransport} = await import("../src/internal/request-context.js");
  const cancellation = new Error("caller stopped request");
  const controller = new AbortController();
  await serve(() => {}, async url => {
    const client = new Arker({apiKey: "synthetic", baseUrl: url, retry: false, fetch});
    const request = withRequestTransport({idleTimeoutMs: 1000, signal: controller.signal}, () => client._request("GET", "/wait"));
    controller.abort(cancellation);
    await assert.rejects(request, error => error === cancellation);
  });
});


test("timeout on retryable status preserves the timeout and does not replay", async () => {
  let requests = 0;
  await serve((_request, response) => { requests++; response.writeHead(503); response.write('{'); }, async url => {
    const {withRequestTransport} = await import("../src/internal/request-context.js");
    const client = new Arker({apiKey: "synthetic", baseUrl: url, fetch});
    await assert.rejects(withRequestTransport({idleTimeoutMs: 50}, () => client._request("GET", "/wait")), RequestIdleTimeoutError);
    assert.equal(requests, 1);
  });
});

test("native execution timeout extends the metadata HTTP setting", async () => {
  let body: any;
  await serve((request, response) => {
    let text = "";
    request.on("data", chunk => {text += chunk;});
    request.on("end", () => { body = JSON.parse(text); const timer = setTimeout(() => response.end(JSON.stringify({exitCode: 0, result: "done"})), 100); response.on("close", () => clearTimeout(timer)); });
  }, async url => {
    assert.equal((await native(url, 30).executeCommand("sleep .1", undefined, undefined, 1)).exitCode, 0);
    assert.equal(body.timeout, 1);
  });
});

for (const lostStartAck of [false, true]) test(`session response timeout preserves owned identity without kill lostStartAck=${lostStartAck}`, async () => {
  const {GuestProcesses} = await import("../src/compat/guest-process.js");
  const original = {exec: GuestProcesses.prototype.exec, kill: GuestProcesses.prototype.kill};
  const killed: string[] = [];
  const timeout = new RequestIdleTimeoutError(50);
  GuestProcesses.prototype.exec = async options => {
    if (lostStartAck) throw timeout;
    return {name: options.name} as any;
  };
  GuestProcesses.prototype.kill = async id => { killed.push(id); return {message: "killed"}; };
  const box: any = {provider: "arker", sandboxId: "s", filesystem: {writeFile: async () => {}}, runCommand: async (command: string) => {
    if (command.includes(" -c ")) return {stdout: '["/home/daytona"]', stderr: "", exitCode: 0};
    throw timeout;
  }};
  try {
    await assert.rejects(new Sandbox(box, undefined, 50).process.createSession("owned"), error =>
      error instanceof DaytonaConnectionTimeoutError && (error as any).sessionId === "owned" && (error as any).processId.startsWith("daytona-session:"));
    assert.deepEqual(killed, []);
  } finally { Object.assign(GuestProcesses.prototype, original); }
});

test("PTY output reader does not inherit metadata timeout context", async () => {
  const {GuestProcesses} = await import("../src/compat/guest-process.js");
  const original = {exec: GuestProcesses.prototype.exec, readOutput: GuestProcesses.prototype.readOutput};
  let readTransport: unknown = "not called";
  GuestProcesses.prototype.exec = async options => ({name: options.name} as any);
  GuestProcesses.prototype.readOutput = async () => {
    readTransport = currentRequestTransport();
    return {data: new Uint8Array(), nextOffset: 0, size: 0, eof: true, exitCode: 0};
  };
  const box: any = {provider: "arker", sandboxId: "s", filesystem: {}, runCommand: async () => ({stdout: '["/home/daytona","/bin/sh"]', stderr: "", exitCode: 0})};
  try {
    const terminal = await new Sandbox(box, undefined, 50).process.createPty({id: "pty", onData: () => {}});
    await terminal.wait();
    assert.equal(readTransport, undefined);
  } finally { Object.assign(GuestProcesses.prototype, original); }
});

for (const operation of ["logs", "createPty", "connectPty"] as const) test(`native ${operation} WebSocket handshake and silence ignore metadata timeout`, async () => {
  const {WebSocketServer} = vendorRequire("isomorphic-ws");
  const server = createServer();
  const sockets = new WebSocketServer({noServer: true});
  const timers: ReturnType<typeof setTimeout>[] = [];
  server.on("upgrade", (request, socket, head) => {
    timers.push(setTimeout(() => sockets.handleUpgrade(request, socket, head, (websocket: any) => {
      if (operation !== "logs") websocket.send(JSON.stringify({type: "control", status: "connected"}));
      timers.push(setTimeout(() => {
        if (operation === "logs") websocket.send(Buffer.concat([Buffer.from([1,1,1]), Buffer.from("late output\n")]));
        else websocket.send(JSON.stringify({type: "control", status: "exited", exitCode: 0}));
        websocket.close(1000);
      }, 140));
    }), 140));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const process = native(`http://127.0.0.1:${(server.address() as {port:number}).port}`, 40);
    const started = Date.now();
    if (operation === "logs") {
      const output: string[] = [];
      await process.getSessionCommandLogs("s", "c", text => {output.push(text);}, () => {});
      assert.equal(output.join(""), "late output\n");
    } else {
      const handle = operation === "createPty" ? await process.createPty({id:"p",onData:()=>{}}) : await process.connectPty("p",{onData:()=>{}});
      try { assert.equal((await handle.wait()).exitCode, 0); }
      finally { await handle.disconnect(); }
    }
    assert.ok(Date.now() - started >= 280);
  } finally {
    timers.forEach(clearTimeout);
    for (const socket of sockets.clients) socket.terminate();
    await new Promise<void>(resolve => sockets.close(() => resolve()));
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});

test("PTY creation does not inherit the unrelated metadata HTTP timeout", async () => {
  const {GuestProcesses} = await import("../src/compat/guest-process.js");
  const original = {exec: GuestProcesses.prototype.exec, readOutput: GuestProcesses.prototype.readOutput};
  const contexts: unknown[] = [];
  GuestProcesses.prototype.exec = async options => {contexts.push(currentRequestTransport()); return {name: options.name} as any;};
  GuestProcesses.prototype.readOutput = async () => ({data:new Uint8Array(),nextOffset:0,size:0,eof:true,exitCode:0});
  const box: any = {provider:"arker",sandboxId:"s",filesystem:{},runCommand:async()=>{
    contexts.push(currentRequestTransport());
    return {stdout:'["/home/daytona","/bin/sh"]',stderr:"",exitCode:0};
  }};
  try {
    await (await new Sandbox(box,undefined,40).process.createPty({id:"p",onData:()=>{}})).wait();
    assert.deepEqual(contexts,[undefined,undefined]);
  } finally {Object.assign(GuestProcesses.prototype,original);}
});

test("callback log readers remain outside metadata timeout scope", async () => {
  const contexts: unknown[] = [];
  const box: any = {provider:"arker",sandboxId:"s",filesystem:{writeFile:async()=>{}},runCommand:async(command:string)=>{
    contexts.push(currentRequestTransport());
    await new Promise(resolve=>setTimeout(resolve,70));
    const payload=JSON.parse(Buffer.from(command.split(" ").at(-1)!.replaceAll("'",""),"base64").toString());
    return {stdout:JSON.stringify({result:{data:payload.stream==="stdout"?Buffer.from("log\n").toString("base64"):"",eof:true}}),stderr:"",exitCode:0};
  }};
  const output:string[]=[];
  await new Sandbox(box,undefined,40).process.getSessionCommandLogs("s","c",text=>{output.push(text);},()=>{});
  assert.deepEqual(contexts,[undefined,undefined]);
  assert.deepEqual(output,["log\n"]);
});
