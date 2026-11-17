// A stateful JavaScript or TypeScript interpreter, run inside a sandbox by the
// compatibility layers: the counterpart of _interpreter.py, with its protocol.
// Each line on stdin is "#" and a JSON request, {"code", "envs_file"} or
// {"restart": true}; each reply is one JSON line, then READY. Code runs in one
// REPL context across requests, as in a notebook, so declarations carry over
// and top-level await works. With the argument "typescript", types are
// stripped first, which needs Node 22.13 or later. Keep it standalone: it
// runs on the sandbox's Node. The TypeScript SDK carries a copy.
"use strict";

const fs = require("node:fs");
const repl = require("node:repl");
const readline = require("node:readline");
const util = require("node:util");
const { PassThrough } = require("node:stream");

const READY = "<arker-interpreter-ready>";
const typescript = process.argv[2] === "typescript";
const write = { stdout: process.stdout.write.bind(process.stdout), stderr: process.stderr.write.bind(process.stderr) };

function newContext() {
  for (const name of Object.keys(require.cache)) delete require.cache[name];
  const server = repl.start({ input: new PassThrough(), output: new PassThrough(), prompt: "", terminal: false });
  // The REPL's own console writes to its output, which capture() does not see.
  server.context.console = console;
  return server;
}

// The REPL reports an error in the code by printing it to its output and
// keeping it as lastError, not through the eval callback.
function evaluate(server, code, cell) {
  return new Promise((resolve) => {
    server.lastError = undefined;
    const failed = () => {
      if (server.lastError === undefined) return;
      server.output.off("data", failed);
      resolve({ error: server.lastError });
    };
    server.output.on("data", failed);
    server.eval(code, server.context, cell, (error, value) => {
      server.output.off("data", failed);
      resolve({ error, value });
    });
  });
}

function strip(code) {
  const { stripTypeScriptTypes } = require("node:module");
  if (!stripTypeScriptTypes) throw new Error("TypeScript needs Node 22.13 or later in the sandbox.");
  return stripTypeScriptTypes(code, { mode: "transform" });
}

// Frames of this file and of Node itself say nothing about the user's code.
function userFrames(stack) {
  return stack
    .split("\n")
    .filter((line) => !(/^\s+at /.test(line) && (line.includes("node:") || line.includes(__filename) || line.includes("<anonymous>"))))
    .join("\n");
}

// Errors from the REPL's context are not instances of this context's Error.
function describe(error) {
  const cause = error instanceof repl.Recoverable ? error.err : error;
  if (cause !== null && typeof cause === "object" && typeof cause.stack === "string") {
    return { name: String(cause.name), value: String(cause.message), traceback: userFrames(cause.stack) };
  }
  return { name: "Error", value: String(cause), traceback: String(cause) };
}

function capture() {
  const output = { stdout: [], stderr: [] };
  for (const name of ["stdout", "stderr"]) {
    process[name].write = (chunk, encoding, callback) => {
      output[name].push(typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8"));
      if (typeof encoding === "function") encoding();
      else if (typeof callback === "function") callback();
      return true;
    };
  }
  return output;
}

function release() {
  process.stdout.write = write.stdout;
  process.stderr.write = write.stderr;
}

// The envs a request's file holds, deleting the file: its values are kept out
// of the request, whose text the Arker API keeps.
function takeEnvs(path) {
  if (!path) return {};
  try {
    return JSON.parse(fs.readFileSync(path, "utf8"));
  } finally {
    fs.unlinkSync(path);
  }
}

async function run(server, request, cell) {
  const environ = { ...process.env };
  Object.assign(process.env, takeEnvs(request.envs_file));
  const output = capture();
  let outcome;
  try {
    outcome = await evaluate(server, typescript ? strip(request.code) : request.code, cell);
  } catch (error) {
    outcome = { error };
  }
  release();
  for (const name of Object.keys(process.env)) if (!(name in environ)) delete process.env[name];
  Object.assign(process.env, environ);
  const results = outcome.error || outcome.value === undefined ? [] : [{ data: { text: util.inspect(outcome.value) }, main: true }];
  return {
    results,
    stdout: output.stdout.join(""),
    stderr: output.stderr.join(""),
    error: outcome.error ? describe(outcome.error) : null,
  };
}

async function main() {
  let server = newContext();
  let count = 0;
  write.stdout(READY);
  for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    const request = JSON.parse(line.replace(/^#/, ""));
    if (request.restart) {
      server.close();
      server = newContext();
      count = 0;
      write.stdout(READY);
      continue;
    }
    count += 1;
    const reply = await run(server, request, "<cell-" + count + ">");
    write.stdout(JSON.stringify({ ...reply, count }) + "\n" + READY);
  }
}

main();
