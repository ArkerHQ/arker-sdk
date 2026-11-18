// Live tests for who may reach an E2B-compatible sandbox from outside: private
// traffic with a traffic access token, and signed file URLs. Run with
// ARKER_API_KEY set.
import assert from "node:assert/strict";

import type { VM, Vm } from "../src/index.js";
import { NotSupportedError, Sandbox } from "../src/e2b.js";
import { fetchStatus, multipart, refusedBy } from "./helpers/fetch-status.js";
import { section } from "./helpers/sections.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP e2b access live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const TOKEN_HEADER = "e2b-traffic-access-token";

async function serve(sandbox: Sandbox, port: number): Promise<void> {
  await sandbox.files.write("/tmp/www/index.html", "served");
  await sandbox.commands.run(`cd /tmp/www && python3 -m http.server ${port}`, { background: true, timeoutMs: 0 });
  await sandbox.commands.run(`for _ in $(seq 50); do curl -sf localhost:${port} > /dev/null && exit 0; sleep 0.2; done; exit 1`);
}

async function portUrl(sandbox: Sandbox, port: number, path = "/index.html"): Promise<string> {
  const hostname = ((await sandbox.arker.refresh()) as VM & Vm).hostname!;
  const dot = hostname.indexOf(".");
  return `https://${hostname.slice(0, dot)}-${port}${hostname.slice(dot)}${path}`;
}

await section("private traffic needs the token", async () => {
  const sandbox = await Sandbox.create({ network: { allowPublicTraffic: false } });
  try {
    const token = sandbox.trafficAccessToken!;
    assert.ok(token);
    await serve(sandbox, 8000);
    const url = `https://${sandbox.getHost(8000)}/index.html`;
    await sandbox.commands.run("true");
    assert.deepEqual(await fetchStatus(url, { headers: { [TOKEN_HEADER]: token } }), [200, "served"]);
    assert.equal((await fetchStatus(url))[0], 403);
    assert.equal((await fetchStatus(url, { headers: { [TOKEN_HEADER]: `${token.slice(0, -1)}x` } }))[0], 403);
    assert.equal((await Sandbox.connect(sandbox.sandboxId)).trafficAccessToken, token);
    await assert.rejects(sandbox.updateNetwork({ allowPublicTraffic: true }), NotSupportedError);

    // A fork keeps the token; a snapshot's sandboxes get their own, and the old one stops working.
    const [fork] = await sandbox.fork();
    assert.ok(fork instanceof Sandbox);
    try {
      assert.equal(fork.trafficAccessToken, token);
      assert.deepEqual(await fetchStatus(await portUrl(fork, 8000), { headers: { [TOKEN_HEADER]: token } }), [200, "served"]);
    } finally {
      await fork.kill();
    }
    const snapshot = await sandbox.createSnapshot();
    try {
      const child = await Sandbox.create(snapshot.snapshotId, { network: { allowPublicTraffic: false } });
      try {
        assert.ok(child.trafficAccessToken && child.trafficAccessToken !== token);
        const childUrl = await portUrl(child, 8000);
        assert.ok([401, 403].includes((await fetchStatus(childUrl, { headers: { [TOKEN_HEADER]: token } }))[0]));
        child.getHost(8000);
        await child.commands.run("true");
        assert.deepEqual(await fetchStatus(childUrl, { headers: { [TOKEN_HEADER]: child.trafficAccessToken } }), [200, "served"]);
        assert.equal((await fetchStatus(childUrl, { headers: { [TOKEN_HEADER]: token } }))[0], 403);
      } finally {
        await child.kill();
      }
    } finally {
      await Sandbox.deleteSnapshot(snapshot.snapshotId);
    }
  } finally {
    await sandbox.kill();
  }
});

await section("public traffic by default", async () => {
  const sandbox = await Sandbox.create();
  try {
    assert.equal(sandbox.trafficAccessToken, undefined);
    await serve(sandbox, 8001);
    const url = `https://${sandbox.getHost(8001)}/index.html`;
    await sandbox.commands.run("true");
    assert.deepEqual(await fetchStatus(url), [200, "served"]);
  } finally {
    await sandbox.kill();
  }
});

await section("signed file urls", async () => {
  const sandbox = await Sandbox.create();
  try {
    await sandbox.files.write("notes/report.txt", "secret report");
    const download = await sandbox.downloadUrl("notes/report.txt");
    assert.deepEqual(await fetchStatus(download), [200, "secret report"]);
    // The URL names one file, with one token, for reading only.
    const query = new URL(download).searchParams;
    assert.equal(query.get("path"), "/home/user/notes/report.txt");
    const signature = query.get("signature")!;
    assert.equal((await fetchStatus(download.replace(encodeURIComponent("/home/user/notes/report.txt"), encodeURIComponent("/etc/hostname"))))[0], 403);
    assert.equal((await fetchStatus(download.replace(signature, `${signature.slice(0, -1)}x`)))[0], 403);
    assert.equal((await fetchStatus(download.split("&signature=")[0]!))[0], 403);
    assert.ok([400, 403].includes((await fetchStatus(`${download}&path=%2Fetc%2Fhostname`))[0]));
    assert.equal((await fetchStatus(download, { method: "POST", body: "overwrite" }))[0], 403);

    const upload = await sandbox.uploadUrl("/tmp/up/data.bin");
    const payload = new Uint8Array(1024).map((_, index) => index % 256);
    const { body, contentType } = multipart("data.bin", payload);
    const [status, reply] = await fetchStatus(upload, { method: "POST", body, headers: { "Content-Type": contentType } });
    assert.equal(status, 200, reply);
    assert.deepEqual(new Uint8Array(await sandbox.files.read("/tmp/up/data.bin", { format: "bytes" })), payload);
    assert.equal((await fetchStatus(upload))[0], 403);

    const expiring = await sandbox.downloadUrl("/home/user/notes/report.txt", { useSignatureExpiration: 5 });
    assert.deepEqual(await fetchStatus(expiring), [200, "secret report"]);
    assert.ok(await refusedBy(expiring, Date.now() + 60_000));

    // A fork does not inherit the URLs, though its file server is running.
    const [fork] = await sandbox.fork();
    assert.ok(fork instanceof Sandbox);
    try {
      const onFork = new URL(download);
      onFork.host = new URL(await portUrl(fork, 49983)).host;
      assert.equal((await fork.commands.run("curl -s -o /dev/null -w '%{http_code}' localhost:49983/files")).stdout, "400");
      assert.ok([401, 403].includes((await fetchStatus(onFork.toString()))[0]));
    } finally {
      await fork.kill();
    }
  } finally {
    await sandbox.kill();
  }
});

console.log("PASS e2b access live");
