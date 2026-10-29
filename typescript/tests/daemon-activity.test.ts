import assert from 'node:assert/strict';
import test from 'node:test';
import type {VM} from '../src/index.js';
import {registeredBootstrap} from '../src/compat/daemon-activity.js';
const identity = {pid: 17, start_ticks: 1234, boot_id: '00000000-0000-0000-0000-000000000001'};

for (const failure of [undefined, new Error('unsupported_operation')]) {
  test(`registration ${failure ? 'failure never admits' : 'precedes run release'}`, async () => {
    const events: string[] = [];
    let acknowledged = false;
    const vm = {id: 'test', baseUrl: 'https://custom.example/api',
      run: async (_: string, options: {time_to_background: number}) => {
        assert.equal(options.time_to_background, 0);return {type: 'background', runId: 'run'};
      },
      getRun: async () => {
        events.push(acknowledged ? 'completed' : 'identity');
        return {state: acknowledged ? 'completed' : 'running', exit_code: 0,
          stdoutBytes: new TextEncoder().encode(JSON.stringify({ready: true, version: 'test', activity: identity})+'\n')};
      },
      sync: async (_: string, body: string, options: {isSecret: boolean}) => {
        assert.equal(options.isSecret, true);acknowledged = true;
        events.push(JSON.parse(body).registered ? 'release' : 'reject');
      },
      _client: {_request: async (method: string, path: string, body: unknown, base: string) => {
        assert.equal(method, 'POST');assert.equal(path, '/v1/vms/test/daemon-activity');
        assert.deepEqual(body, identity);assert.equal(base, 'https://custom.example/api');
        assert.equal(acknowledged, false);events.push('register');if(failure) throw failure;
      }},
    } as unknown as VM;
    const call = registeredBootstrap(vm, 'bootstrap', '/private/request.json', 'test');
    if (failure) {
      await assert.rejects(call, error => error === failure);
      assert.deepEqual(events, ['identity', 'register', 'reject']);
    } else {
      assert.deepEqual((await call).activity, identity);
      assert.deepEqual(events, ['identity', 'register', 'release', 'completed']);
    }
  });
}

test('retained ready output from an exited guard cannot authorize registration', async () => {
  let registered = false;
  const vm = {
    run: async () => ({type: 'background', runId: 'run'}),
    getRun: async () => ({state: 'completed', stdoutBytes: new TextEncoder().encode(JSON.stringify({ready: true, version: 'test', activity: identity})+'\n')}),
    sync: async () => {},
    _client: {_request: async () => { registered = true; }},
  } as unknown as VM;
  await assert.rejects(registeredBootstrap(vm, 'bootstrap', '/private/request.json', 'test'), /released its activity guard/);
  assert.equal(registered, false);
});

for (const where of ['poll', 'ack', 'cancel']) {
  test(`${where} failure preserves primary and failed negative acknowledgement`, async () => {
    const primary = where === 'cancel' ? new DOMException('cancelled', 'AbortError') : new Error('primary');
    const cleanup = new Error('negative acknowledgement failed');
    const vm = {
      id: 'test', baseUrl: 'https://custom.example/api',
      run: async () => ({type: 'background', runId: 'run'}),
      getRun: async () => {
        if (where !== 'ack') throw primary;
        return {state: 'running', stdoutBytes: new TextEncoder().encode(JSON.stringify({ready: true, version: 'test', activity: identity})+'\n')};
      },
      sync: async (_: string, body: string) => { throw JSON.parse(body).registered ? primary : cleanup; },
      _client: {_request: async () => {}},
    } as unknown as VM;
    await assert.rejects(registeredBootstrap(vm, 'bootstrap', '/private/request.json', 'test'), error => {
      assert.ok(error instanceof AggregateError);
      assert.deepEqual(error.errors, [primary, cleanup]);return true;
    });
  });
}
