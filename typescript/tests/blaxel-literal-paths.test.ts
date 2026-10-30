import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import { connect } from "node:net";
import { createBlaxelDataPlane } from "../src/compat/blaxel-data-plane.js";
import { listen, closeServer } from "./provider-fixtures.js";

// Assert the native encoded requests, including operations whose path rules differ.
test("native Blaxel preserves literal filesystem paths and options", async t => {
  const calls: Array<{url: string; method: string; body: string}> = [];
  const server = createServer(async (req, res) => {
    let body = "";
    for await (const part of req) body += part;
    calls.push({ url: req.url!, method: req.method!, body });
    res.writeHead(200, {"content-type": "application/json"});
    res.end(JSON.stringify({name: "tree", path: "/~/tree", files: [], subdirectories: [], message: "success", content: "value"}));
  });
  t.after(() => closeServer(server));
  const address = await listen(server);
  const plane = createBlaxelDataPlane({connectPort: async () => connect(address.port, "127.0.0.1")});
  t.after(() => plane.close());
  for (const path of ["~", "~/a", "~root/a", "~//a", "~/../a"])
    await plane.fs.read(path);
  assert.deepEqual(calls.splice(0).map(call => call.url), ["/filesystem/~", "/filesystem/~%2Fa", "/filesystem/~root%2Fa", "/filesystem/~%2F%2Fa", "/filesystem/~%2F..%2Fa"]);
  await plane.fs.mkdir("private", "0700");
  assert.equal(JSON.parse(calls.at(-1)!.body).permissions, "0700");
  await plane.fs.rm("private");
  assert(!calls.at(-1)!.url.includes("recursive=true"));
  await plane.fs.rm("private", true);
  assert(calls.at(-1)!.url.includes("recursive=true"));
  await plane.fs.writeTree([{ path: "file", content: "value" }], "~/tree");
  assert.deepEqual(JSON.parse(calls.at(-1)!.body), {files: {file: "value"}});
});
