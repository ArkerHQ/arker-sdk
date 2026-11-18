// The Python interpreter behind code contexts: a copy of the Python SDK's
// python/src/arker/_interpreter.py, which documents it. Keep the two identical.
export const INTERPRETER = String.raw`"""A stateful Python interpreter, run inside a sandbox by the compatibility layers.

Uploaded to the VM and started in an Arker session of its own. Each line on
stdin is "#" and a JSON request, {"code", "envs_file"} or {"restart": true}; each
reply is one JSON line, then READY, the prompt an Arker run waits for. The "#"
makes a request a comment should the interpreter have stopped and the
session's shell read it instead. Code shares one namespace across requests,
as in a notebook. Keep it standalone: it runs on the sandbox's Python, which
may be older than this SDK's. The TypeScript SDK carries a copy.
"""

import ast
import base64
import io
import json
import linecache
import os
import sys
import tempfile
import traceback

READY = "<arker-interpreter-ready>"
# _repr_*_ methods, as Jupyter reads them, and the name each format has in a result.
FORMATS = ("html", "markdown", "svg", "png", "jpeg", "pdf", "latex", "json", "javascript")

# What the current request displayed, in order.
results = []
namespace = {"__name__": "__main__"}


def represent(value):
    data = {"text": repr(value)}
    for name in FORMATS:
        method = getattr(type(value), f"_repr_{name}_", None)
        try:
            output = method(value) if callable(method) else None
        except Exception:
            output = None
        if isinstance(output, bytes):
            output = base64.b64encode(output).decode()
        if output is not None:
            data[name] = output
    return data


def figures():
    """Open matplotlib figures, as PNG results, the way a notebook shows them."""
    pyplot = sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return []
    images = []
    for number in pyplot.get_fignums():
        buffer = io.BytesIO()
        pyplot.figure(number).savefig(buffer, format="png")
        images.append({"text": repr(pyplot.figure(number)), "png": base64.b64encode(buffer.getvalue()).decode()})
    pyplot.close("all")
    return images


def display(*values):
    results.extend({"data": represent(value), "main": False} for value in values)


def execute(code, cell):
    linecache.cache[cell] = (len(code), None, code.splitlines(True), cell)
    tree = ast.parse(code, cell)
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    exec(compile(tree, cell, "exec"), namespace)
    value = eval(compile(ast.Expression(last.value), cell, "eval"), namespace) if last else None
    results.extend({"data": data, "main": False} for data in figures())
    if value is not None:
        results.append({"data": represent(value), "main": True})


def describe(error):
    # Start at the user's code, not at this file; a syntax error has none.
    trace = None if isinstance(error, SyntaxError) else error.__traceback__
    while trace is not None and trace.tb_frame.f_code.co_filename == __file__:
        trace = trace.tb_next
    lines = traceback.format_exception(type(error), error, trace)
    return {"name": type(error).__name__, "value": str(error), "traceback": "".join(lines)}


def capture():
    """Point stdout and stderr, of this process and its children, at temporary files."""
    sys.stdout.flush()
    sys.stderr.flush()
    files = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]  # noqa: SIM115 - closed by release()
    saved = [os.dup(1), os.dup(2)]
    os.dup2(files[0].fileno(), 1)
    os.dup2(files[1].fileno(), 2)
    return files, saved


def release(files, saved):
    sys.stdout.flush()
    sys.stderr.flush()
    output = []
    for fd, copy, file in zip((1, 2), saved, files):  # noqa: B905 - runs on Pythons without strict=
        os.dup2(copy, fd)
        os.close(copy)
        file.seek(0)
        output.append(file.read().decode("utf-8", "replace"))
        file.close()
    return output


def take_envs(path):
    """The envs a request's file holds, deleting the file: its values are kept
    out of the request, whose text the Arker API keeps."""
    if not path:
        return {}
    try:
        with open(path) as file:
            return json.load(file)
    finally:
        os.unlink(path)


def main():
    os.environ.setdefault("MPLBACKEND", "Agg")
    namespace["display"] = display
    count = 0
    sys.stdout.write(READY)
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        request = json.loads(line.lstrip("#"))
        if request.get("restart"):
            os.execv(sys.executable, [sys.executable, "-u", __file__])
        count += 1
        del results[:]
        environ = dict(os.environ)
        os.environ.update(take_envs(request.get("envs_file")))
        files, saved = capture()
        error = None
        try:
            execute(request["code"], f"<cell-{count}>")
        except BaseException as exc:
            error = describe(exc)
        stdout, stderr = release(files, saved)
        os.environ.clear()
        os.environ.update(environ)
        reply = {"results": list(results), "stdout": stdout, "stderr": stderr, "error": error, "count": count}
        # Escaped, so output that prints READY cannot end the request early.
        sys.stdout.write(json.dumps(reply).replace("<", "\\u003c") + "\n" + READY)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
`;

// The JavaScript and TypeScript interpreter behind code contexts: a copy of the
// Python SDK's python/src/arker/_interpreter.js, which documents it. Keep the two identical.
export const NODE_INTERPRETER = String.raw`// A stateful JavaScript or TypeScript interpreter, run inside a sandbox by the
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
`;

// The file server behind signed file URLs: a copy of the Python SDK's
// python/src/arker/_file_server.py, which documents it. Keep the two identical.
export const FILE_SERVER = String.raw`"""The file server behind signed file URLs, run inside a sandbox by the compatibility layers.

Arker's inbound rules decide who reaches it: each signed URL is a rule that
lets in one method, on /files, with one path and one token, until it expires.
This server only reads and writes the file, and refuses a query that names a
parameter twice, so the path it serves is the one the rule matched. GET
returns the file; POST writes the body, or the first file of a
multipart/form-data body, to it. Keep it standalone: it runs on the sandbox's
Python, which may be older than this SDK's. The TypeScript SDK carries a copy.
"""

import json
import os
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def multipart_file(content_type, body):
    """The first file in a multipart/form-data body, else its first part."""
    params = dict(param.strip().split("=", 1) for param in content_type.split(";")[1:] if "=" in param)
    boundary = params.get("boundary", "").strip('"').encode()
    if not boundary:
        return None
    parts = []
    for part in body.split(b"--" + boundary)[1:-1]:
        head, _, data = part.partition(b"\r\n\r\n")
        parts.append((head, data[:-2] if data.endswith(b"\r\n") else data))
    files = [data for head, data in parts if b"filename=" in head]
    return (files or [data for _, data in parts] or [None])[0]


class Handler(BaseHTTPRequestHandler):
    def target(self):
        url = urllib.parse.urlsplit(self.path)
        pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        names = [name for name, _ in pairs]
        if url.path != "/files" or len(names) != len(set(names)) or "path" not in names:
            self.reply(400, {"message": "Expected /files with each query parameter once, path among them."})
            return None
        return dict(pairs)["path"]

    def reply(self, status, body, content_type="application/json"):
        data = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.target()
        if path is None:
            return
        try:
            handle = open(path, "rb")  # noqa: SIM115 - closed below
        except OSError as error:
            self.reply(404, {"message": f"Cannot read {path}: {error.strerror}"})
            return
        with handle:
            name = urllib.parse.quote(os.path.basename(path))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
            self.end_headers()
            shutil.copyfileobj(handle, self.wfile)

    def do_POST(self):
        path = self.target()
        if path is None:
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        content_type = self.headers.get("Content-Type") or ""
        if content_type.startswith("multipart/form-data"):
            body = multipart_file(content_type, body)
            if body is None:
                self.reply(400, {"message": "The multipart body has no file."})
                return
        try:
            os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(body)
        except OSError as error:
            self.reply(400, {"message": f"Cannot write {path}: {error.strerror}"})
            return
        self.reply(200, [{"name": os.path.basename(path), "type": "file", "path": path}])

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
`;
