// Live tests for the E2B Code Interpreter compatibility layer: common code,
// unchanged except for its imports, against a real deployment. Run with
// ARKER_API_KEY set.
import assert from "node:assert/strict";

import { NotSupportedError, Sandbox, TimeoutError, type OutputMessage } from "../src/e2b-code-interpreter.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP e2b code interpreter live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const sandbox = await Sandbox.create();
try {
  // run code keeps state
  const printed: string[] = [];
  await sandbox.runCode("x = 40\nprint('hi')", { onStdout: (message: OutputMessage) => void printed.push(message.line) });
  assert.deepEqual(printed, ["hi\n"]);
  const sum = await sandbox.runCode("x + 2");
  assert.deepEqual([sum.text, sum.results[0]!.isMainResult, sum.error], ["42", true, undefined]);
  const environment = await sandbox.runCode("import os\nprint(os.environ['RUN_VAR'], os.getcwd())", { envs: { RUN_VAR: "v" } });
  assert.deepEqual(environment.logs.stdout, ["v /home/user\n"]);
  const failed = await sandbox.runCode("x / 0");
  assert.deepEqual([failed.error?.name, failed.error?.value], ["ZeroDivisionError", "division by zero"]);
  assert.match(failed.error!.traceback, /x \/ 0/);
  const html = await sandbox.runCode("class Bold:\n    def _repr_html_(self):\n        return '<b>x</b>'\nBold()");
  assert.equal(html.results[0]!.html, "<b>x</b>");
  await assert.rejects(sandbox.runCode("1", { language: "r" }), NotSupportedError);

  // output cannot end a run early
  const marker = await sandbox.runCode("print('<arker-interpreter-ready>')\n1 + 1");
  assert.deepEqual([marker.logs.stdout, marker.text], [["<arker-interpreter-ready>\n"], "2"]);
  assert.equal((await sandbox.runCode("3")).text, "3");

  // code contexts
  const context = await sandbox.createCodeContext({ cwd: "/tmp" });
  await sandbox.runCode("y = 1", { context });
  assert.equal((await sandbox.runCode("import os\n(y, os.getcwd())", { context })).text, "(1, '/tmp')");
  assert.equal((await sandbox.runCode("'y' in globals()")).text, "False");
  assert.ok((await sandbox.listCodeContexts()).some((listed) => listed.id === context.id));
  await sandbox.restartCodeContext(context);
  assert.equal((await sandbox.runCode("'y' in globals()", { context })).text, "False");
  await assert.rejects(sandbox.runCode("import time\ntime.sleep(30)", { context, timeoutMs: 2_000 }), TimeoutError);
  assert.equal((await sandbox.runCode("1 + 1", { context })).text, "2");
  await sandbox.removeCodeContext(context);
  assert.ok(!(await sandbox.listCodeContexts()).some((listed) => listed.id === context.id));

  // JavaScript and TypeScript
  const logged: string[] = [];
  await sandbox.runCode("const x = 40; console.log('hi')", { language: "js", onStdout: (message: OutputMessage) => void logged.push(message.line) });
  assert.deepEqual(logged, ["hi\n"]);
  const awaited = await sandbox.runCode("await new Promise((done) => setTimeout(done, 10)); x + 2", { language: "javascript" });
  assert.deepEqual([awaited.text, awaited.results[0]!.isMainResult, awaited.error], ["42", true, undefined]);
  assert.equal((await sandbox.runCode("process.env.RUN_VAR + ' ' + process.cwd()", { language: "js", envs: { RUN_VAR: "v" } })).text, "'v /home/user'");
  const thrown = await sandbox.runCode("null.x", { language: "js" });
  assert.equal(thrown.error?.name, "TypeError");
  assert.match(thrown.error!.traceback, /<cell-/);
  const typed = await sandbox.runCode("const n: number = 2; interface P { n: number }; n * 21", { language: "ts" });
  assert.deepEqual([typed.text, typed.error], ["42", undefined]);
  // Each language has its own default context, and a context keeps its language.
  assert.equal((await sandbox.runCode("typeof x", { language: "ts" })).text, "'undefined'");
  assert.equal((await sandbox.runCode("typeof n", { language: "js" })).text, "'undefined'");
  const jsContext = await sandbox.createCodeContext({ language: "js" });
  assert.equal(jsContext.language, "javascript");
  await sandbox.runCode("let kept = 'yes'", { context: jsContext });
  assert.equal((await sandbox.runCode("kept", { context: jsContext })).text, "'yes'");
  const languages = new Set((await sandbox.listCodeContexts()).map((listed) => listed.language));
  assert.ok(languages.has("javascript") && languages.has("typescript"));
  await sandbox.restartCodeContext(jsContext);
  assert.equal((await sandbox.runCode("typeof kept", { context: jsContext })).text, "'undefined'");
  await assert.rejects(sandbox.runCode("await new Promise(() => {})", { context: jsContext, timeoutMs: 2_000 }), TimeoutError);
  assert.equal((await sandbox.runCode("typeof process", { context: jsContext })).text, "'object'");
  await sandbox.removeCodeContext(jsContext);

  // charts
  await sandbox.commands.run("python3 -m pip install --quiet matplotlib", { timeoutMs: 300_000 });
  const chart = await sandbox.runCode("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.show()");
  assert.ok(chart.results[0]!.png?.startsWith("iVBORw0KGgo"));

  // reconnecting keeps the default context
  const again = await Sandbox.connect(sandbox.sandboxId);
  assert.equal((await again.runCode("x")).text, "40");
} finally {
  await sandbox.kill();
}

// secret values stay out of what the runs API returns
const secret = `secret-${crypto.randomUUID()}`;
// The API redacts what looks like an assignment in a command, but only its first line.
const key = `-----BEGIN KEY-----\n${secret}\n-----END KEY-----\n`;
const secrets = await Sandbox.create();
try {
  assert.equal((await secrets.commands.run('printf %s "$KEY"', { envs: { KEY: key } })).stdout, key);
  const handle = await secrets.commands.run("sleep 600", { background: true, envs: { KEY: key }, timeoutMs: 0 });
  assert.deepEqual(
    (await secrets.commands.list()).filter((info) => info.pid === handle.pid).map((info) => info.envs),
    [{ KEY: key }],
  );
  await handle.kill();
  await assert.rejects(secrets.git.clone("http://127.0.0.1:9/repo.git", { path: "/tmp/repo", username: "someone", password: secret }));
  await secrets.git.dangerouslyAuthenticate({ username: "someone", password: secret, host: "example.com" });
  assert.equal((await secrets.runCode("import os\nos.environ['KEY']", { envs: { KEY: key } })).text, `'${key.replaceAll("\n", "\\n")}'`);
  const runs = [];
  let cursor: string | undefined;
  do {
    const page = await secrets.arker.listRuns({ cursor });
    runs.push(...page.runs);
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  assert.deepEqual(runs.filter((run) => run.command?.includes(secret)).map((run) => run.command), []);
  // Only the two commands that print it on purpose output it.
  const outputs = await Promise.all(runs.map(async (run) => (await secrets.arker.getRun(run.run_id)).stdout));
  assert.equal(outputs.filter((stdout) => stdout.includes(secret)).length, 2);
  // Nor does any file another user could read.
  assert.equal((await secrets.commands.run(`grep -rlF ${secret.slice(0, -1)} /tmp /etc /home 2>/dev/null; true`)).stdout, "");
} finally {
  await secrets.kill();
}

console.log("PASS e2b code interpreter live");
