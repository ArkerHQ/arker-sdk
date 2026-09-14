import assert from 'node:assert/strict';
import { Arker } from '../src/index';

for (const size of [0, 5, 20 * 1024 * 1024 + 1]) {
  {
    const data = new Uint8Array(size).fill(0xff);
    const chunks: Buffer[] = [];
    const ids = new Set<string>();
    let received = 0;
    const client = new Arker({ apiKey: 'test', baseUrl: 'https://test.invalid/api', retry: false,
      fetch: async (input, init) => {
        assert.equal(new URL(String(input)).pathname, '/api/v1/vms/vm_1/sync');
        const body = JSON.parse(String(init?.body));
        assert.equal(body.op, 'write');
        const results = body.writes.map((entry: any) => {
          const chunk = Buffer.from(entry.content, 'base64');
          assert.ok(chunk.length <= 5 * 1024 * 1024);
          assert.equal(entry.path, '/tmp/probe');
          assert.equal(entry.size, size);
          assert.equal(entry.start, received);
          chunks.push(chunk); received += chunk.length;
          assert.equal(entry.end, received); ids.add(entry.upload_id);
          return { path: entry.path, size, complete: received === size, written: received === size };
        });
        return Response.json({ok: true, op: "write", results});
      },
    });
    await client.vm('vm_1').sync('/tmp/probe', data);
    assert.deepEqual(Buffer.concat(chunks), Buffer.from(data));
    assert.equal(ids.size, 1);
  }
}

for (const result of [{ complete: false, written: false }, { complete: true, written: false }]) {
  const client = new Arker({ apiKey: "test", baseUrl: "https://test.invalid/api", retry: false,
    fetch: async () => Response.json({ ok: true, op: "write", results: [result] }),
  });
  await assert.rejects(() => client.vm("vm_1").sync("/tmp/x", "x"), /did not complete/);
}
console.log("PASS sync routes: empty, small, large, and unfinished uploads");
