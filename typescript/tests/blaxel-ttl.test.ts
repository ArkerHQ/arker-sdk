import assert from 'node:assert/strict';
import {test, type TestContext} from 'node:test';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {SandboxInstance as Native} from '@blaxel/core';
import {SandboxInstance as Shim} from '../src/blaxel.js';
import {blaxelLifetimePolicies} from '../src/compat/blaxel-create.js';
import {preserveProperties} from './provider-fixtures.js';
const cases = require('../../tests/fixtures/blaxel-ttl.json') as {
  durations: Array<{options: {ttl?: unknown}; seconds?: number | null; reject?: boolean}>;
  policies: Array<{config: Record<string, unknown>; reject?: boolean}>;
};
function router(t: TestContext, provider: string) {
  const directory = mkdtempSync(join(tmpdir(), 'blaxel-ttl-'));
  t.after(() => rmSync(directory, {recursive:true, force:true}));
  const path = join(directory, 'router.json');
  writeFileSync(path, JSON.stringify({version:1, create:{strategy:'priority',order:[provider]},existing:{strategy:'priority',order:[provider]}}));
  return path;
}

test('TTL native route preserves original values and presence', async t => {
  preserveProperties(t, Native, 'create');
  const routerConfigPath = router(t, 'blaxel');
  const observed = new Error('captured native call');
  for (const {options} of cases.durations) {
    Native.create = async input => {
      assert.equal(Object.hasOwn(input!, 'ttl'), Object.hasOwn(options, 'ttl'));
      assert.equal(Reflect.get(input!, 'ttl'), options.ttl);
      throw observed;
    };
    await assert.rejects(Shim.create(options as never, {routerConfigPath, apiKey:'test', workspace:'test'}), error => error === observed);
  }
});

test('TTL Arker route uses exact creation schedule without clamping', async t => {
  const routerConfigPath = router(t, 'arker');
  for (const row of cases.durations) {
    let captured: Record<string, unknown> | undefined;
    await assert.rejects(Shim.create(row.options as never, {routerConfigPath, arker:{apiKey:'test', source:'source',
      baseUrl:'https://custom.example/api', policies:{rules:{'customer-deny':{type:'inbound',action:'deny',priority:10}}},
      fetch: (async (_url, init) => {
        captured = JSON.parse(String(init?.body)).policies.rules;
        return new Response(JSON.stringify({error:{code:'forbidden',message:'fixture',timestamp:'2026-01-01T00:00:00Z',request_id:'test',request:{kind:'matched',operation_id:'fork'}}}), {status:403,headers:{'content-type':'application/json'}});
      }) as typeof fetch,
    }}));
    assert.equal(captured !== undefined, !row.reject);
    if (captured) {
      assert.deepEqual(captured['customer-deny'], {type:'inbound',action:'deny',priority:10});
      assert.deepEqual(captured['computesdk-blaxel-lifetime'], row.seconds == null ? undefined : {type:'schedule',after_created:row.seconds,action:'delete'});
    }
  }
});

test('TTL policy conflicts and nullable rules share the Python contract', () => {
  for (const row of cases.policies) {
    const before = structuredClone(row.config);
    if (row.reject) assert.throws(() => blaxelLifetimePolicies('5m', row.config as never));
    else assert.deepEqual(blaxelLifetimePolicies('5m', row.config as never), {rules:{'computesdk-blaxel-lifetime':{type:'schedule',after_created:300,action:'delete'}}});
    assert.equal(blaxelLifetimePolicies(undefined, row.config as never), undefined);
    assert.deepEqual(row.config, before);
  }
});
