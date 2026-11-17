// Live tests for signed URLs in the Daytona compatibility layer: file download
// and upload URLs, and signed preview URLs. Run with ARKER_API_KEY set.
import assert from "node:assert/strict";

import type { VM, Vm } from "../src/index.js";
import { Daytona, DaytonaValidationError } from "../src/daytona.js";
import { fetchStatus, multipart, refusedBy } from "./helpers/fetch-status.js";
import { section } from "./helpers/sections.js";

if (!process.env.ARKER_API_KEY) {
  console.log("SKIP daytona access live: set ARKER_API_KEY to run");
  process.exit(0);
}
if (!process.env.ARKER_BASE_URL) {
  process.env.ARKER_PROVIDER ??= "aws";
  process.env.ARKER_REGION ??= "us-east-1";
}

const refused = (status: number) => status === 401 || status === 403;

const sandbox = await new Daytona().create();
try {
  await section("file urls", async () => {
    await sandbox.fs.uploadFile(Buffer.from("daytona report"), "/tmp/report.txt");
    const download = await sandbox.downloadUrl("/tmp/report.txt");
    assert.deepEqual(await fetchStatus(download), [200, "daytona report"]);
    assert.equal((await fetchStatus(download.replace("signature=", "signature=x")))[0], 403);
    assert.equal((await fetchStatus(download, { method: "POST", body: "overwrite" }))[0], 403);
    await assert.rejects(sandbox.downloadUrl("report.txt"), DaytonaValidationError);

    const upload = await sandbox.uploadUrl("/tmp/in/data.bin");
    const payload = new Uint8Array(256).map((_, index) => index);
    const { body, contentType } = multipart("data.bin", payload);
    assert.equal((await fetchStatus(upload, { method: "POST", body, headers: { "Content-Type": contentType } }))[0], 200);
    assert.deepEqual(new Uint8Array(await sandbox.fs.downloadFile("/tmp/in/data.bin")), payload);

    const short = await sandbox.downloadUrl("/tmp/report.txt", 3);
    assert.equal((await fetchStatus(short))[0], 200);
    assert.ok(await refusedBy(short, Date.now() + 60_000));

    const forever = await sandbox.downloadUrl("/tmp/report.txt", 0);
    assert.equal((await fetchStatus(forever))[0], 200);
    await sandbox.rotateSigningKey();
    assert.ok(refused((await fetchStatus(download))[0]));
    assert.ok(refused((await fetchStatus(forever))[0]));
    assert.deepEqual(await fetchStatus(await sandbox.downloadUrl("/tmp/report.txt")), [200, "daytona report"]);
  });

  await section("signed preview urls", async () => {
    await sandbox.fs.uploadFile(Buffer.from("preview"), "/tmp/www/index.html");
    await sandbox.process.executeCommand("cd /tmp/www && (nohup python3 -m http.server 3000 > /dev/null 2>&1 &)");
    await sandbox.process.executeCommand("for _ in $(seq 50); do curl -sf localhost:3000 > /dev/null && exit 0; sleep 0.2; done");
    assert.equal(sandbox.public, false);
    const signed = await sandbox.getSignedPreviewUrl(3000, 600);
    assert.deepEqual([signed.sandboxId, signed.port], [sandbox.id, 3000]);
    assert.deepEqual(await fetchStatus(signed.url), [200, "preview"]);
    assert.deepEqual(await fetchStatus(signed.url.replace("/?", "/index.html?")), [200, "preview"]);
    assert.ok(refused((await fetchStatus(signed.url.replace(signed.token, `${signed.token.slice(0, -1)}x`)))[0]));
    assert.ok(refused((await fetchStatus(signed.url.split("?")[0]!))[0]));
    const other = await sandbox.getSignedPreviewUrl(3000);
    await sandbox.expireSignedPreviewUrl(3000, signed.token);
    assert.ok(refused((await fetchStatus(signed.url))[0]));
    assert.deepEqual(await fetchStatus(other.url), [200, "preview"]);
    const brief = await sandbox.getSignedPreviewUrl(3000, 3);
    assert.ok(await refusedBy(brief.url, Date.now() + 60_000));
    // A fork does not inherit the signed URL, though it serves the port.
    const fork = await sandbox.fork();
    try {
      const hostname = ((await fork.arker.refresh()) as VM & Vm).hostname!;
      const dot = hostname.indexOf(".");
      const onFork = `https://${hostname.slice(0, dot)}-3000${hostname.slice(dot)}/?arker_token=${other.token}`;
      assert.ok(refused((await fetchStatus(onFork))[0]));
      assert.equal((await fork.process.executeCommand("curl -sf localhost:3000")).result, "preview");
    } finally {
      await fork.delete();
    }
  });
} finally {
  await sandbox.delete();
}

console.log("PASS daytona access live");
