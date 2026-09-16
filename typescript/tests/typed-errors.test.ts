import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Arker, ArkerError } from '../src/index';

const raw = {
  code: 'not_found', message: 'missing', timestamp: '2026-09-16T00:00:00Z',
  request_id: 'req-test', request: { kind: 'matched', operation_id: 'deleteVm' },
  details: { resource: 'vm' },
};

test('HTTP reader preserves the generated error body', async () => {
  const client = new Arker({apiKey: 'k', baseUrl: 'https://test.invalid', retry: false,
    fetch: (async () => Response.json({error: raw}, {status: 404})) as typeof fetch});
  try { await client.vm('vm').delete(); throw new Error('expected error'); }
  catch (error) {
    assert.ok(error instanceof ArkerError);
    assert.deepEqual(error.body, raw);
  }
});

test('unknown errors preserve their original fields', async () => {
  const future = {...raw, code: 'future_code', extra: 42};
  const client = new Arker({apiKey: 'k', baseUrl: 'https://test.invalid', retry: false,
    fetch: (async () => Response.json({error: future}, {status: 409})) as typeof fetch});
  try { await client.vm('vm').delete(); throw new Error('expected error'); }
  catch (error) {
    assert.ok(error instanceof ArkerError);
    assert.equal(error.body, undefined);
    assert.deepEqual(error.raw, future);
  }
});

for (const work of ['unknown', 'continuing', 'stopped']) {
  test(`partial ${work} work is not replayed`, async () => {
    let calls = 0;
    const partial = {...raw, code: 'unavailable', details: undefined, retry_after_seconds: 0,
      recovery: {work, context: {vm_id: 'vm-existing'}}};
    const client = new Arker({apiKey: 'k', baseUrl: 'https://test.invalid', retry: {attempts: 3},
      fetch: (async () => {calls++; return Response.json({error: partial}, {status: 503});}) as typeof fetch});
    await assert.rejects(client.vm('vm').delete(), (error: unknown) =>
      error instanceof ArkerError && error.body?.recovery?.context?.vm_id === 'vm-existing');
    assert.equal(calls, 1);
  });
}

test('all HTTP contract examples retain their typed payload', async () => {
  const { readFileSync } = await import('node:fs');
  const spec = JSON.parse(readFileSync(new URL('../../openapi.json', import.meta.url), 'utf8'));
  for (const response of Object.values(spec.components.responses) as any[]) {
    for (const item of Object.values(response.content?.['application/json']?.examples ?? {}) as any[]) {
      if (!item.value?.error) continue;
      const payload = item.value.error;
      const client = new Arker({apiKey: 'k', baseUrl: 'https://test.invalid', retry: false,
        fetch: (async () => Response.json({error: payload}, {status: 400})) as typeof fetch});
      await assert.rejects(client.vm('vm').delete(), (error: unknown) => {
        assert.ok(error instanceof ArkerError);
        assert.deepEqual(error.body, payload);
        return true;
      });
    }
  }
});

test('per-file sync failures retain recovery handles', async () => {
  const fileError = {code: 'not_found', message: 'missing file', details: {resource: 'file'},
    recovery: {work: 'stopped', context: {vm_id: 'vm'}}};
  const client = new Arker({apiKey: 'k', baseUrl: 'https://test.invalid', retry: false,
    fetch: (async () => Response.json({op: 'write', results: [{error: fileError}]})) as typeof fetch});
  await assert.rejects(client.vm('vm').sync('/file', new Uint8Array([1])), (error: unknown) => {
    assert.ok(error instanceof ArkerError);
    assert.deepEqual(error.body, fileError);
    return true;
  });
});
