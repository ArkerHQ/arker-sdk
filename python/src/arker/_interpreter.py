"""A stateful Python interpreter, run inside a sandbox by the compatibility layers.

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
