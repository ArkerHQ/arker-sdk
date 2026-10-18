// The execution-model contract: VM and session states, the policy shapes that
// suspend, delete and gate on `vm_state`, and the 409s a state refusal raises.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  Arker, ArkerError,
  type PolicyAction, type PolicyRule, type PolicyUpdate, type ScheduleRule, type SessionState, type VmState,
} from '../src/index';

const sshKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl dev@laptop';

const rules = {
  'dev-key': {type: 'inbound', priority: 50, match: {via: ['ssh'], ssh_keys: [sshKey]}, action: 'allow'},
  shell: {type: 'inbound', priority: 40,
    match: {via: ['ssh', 'pty'], connection_state: ['new', 'open'], vm_state: 'running'}, action: 'allow'},
  'idle-gate': {type: 'inbound', priority: 30, match: {ports: [8080], vm_state: 'idle'}, action: 'deny'},
  web: {type: 'inbound', priority: 20, match: {ports: [8080], vm_state: 'running'}, action: 'allow'},
  llm: {type: 'outbound', priority: 10, match: {hosts: ['api.example']},
    action: {rewrite: {headers: {authorization: '${secret:KEY}'}}, suspend: true}},
  legacy: {type: 'outbound', priority: 5, action: {scaling: {suspend: true}}},
  rest: {type: 'outbound', priority: 0, action: 'suspend'},
  nap: {type: 'schedule', after_last_running_start: 600, action: 'suspend'},
  ttl: {type: 'schedule', after_created: 86400, action: 'delete'},
  end: {type: 'schedule', at: '2030-01-01T00:00:00Z', action: 'delete'},
  now: {type: 'schedule', at: '2020-01-01T00:00:00Z', action: 'suspend'},
} satisfies PolicyUpdate['rules'];

// Compile-time: exactly the contract's states and policy shapes.
const vmStates: VmState[] = ['idle', 'running'];
const sessionStates: SessionState[] = ['idle', 'running', 'pending'];
const verbs: PolicyAction[] = ['allow', 'deny', 'delete', 'suspend'];
// @ts-expect-error a VM is never pending
const noPendingVm: VmState = 'pending';
// @ts-expect-error there is no scale action
const noScale: PolicyAction = 'scale';
// @ts-expect-error there is no webhook action
const noWebhook: PolicyAction = {webhook: {url: 'https://hook.example'}};
// @ts-expect-error there is no after_wake clock
const noAfterWake: ScheduleRule = {type: 'schedule', after_wake: 60, action: 'suspend'};
// @ts-expect-error a request arrives by port, ssh or pty only
const noVia: PolicyRule = {type: 'inbound', priority: 1, match: {via: ['http']}, action: 'allow'};
// @ts-expect-error a connection is new or open only
const noConnectionState: PolicyRule = {type: 'inbound', priority: 1, match: {connection_state: ['closed']}, action: 'deny'};
// @ts-expect-error there is no resources rule
const noResourcesRule: PolicyRule = {type: 'resources', priority: 1, action: 'suspend'};
void [vmStates, sessionStates, verbs, noPendingVm, noScale, noWebhook, noAfterWake, noResourcesRule, noVia, noConnectionState];

type Call = {method: string; url: string; body?: unknown};

function client(status: number, body: unknown, calls: Call[] = []): Arker {
  return new Arker({apiKey: 'k', baseUrl: 'https://test.invalid', retry: false,
    fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({method: init?.method ?? 'GET', url: String(input),
        body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined});
      return Response.json(body, {status});
    }) as typeof fetch});
}

const vm = (state: VmState) => ({
  vm_id: 'vm_1', owner_org_id: 'org', created_at: '2026-10-16T00:00:00Z', description: null, public: false,
  state, network: {}, resources: {}, sessions: [{session_id: 's1', state: 'pending', cwd: '/'}],
  policies: {rules},
});

function refusal(code: string, request: object, details: object) {
  return {error: {code, message: 'refused', timestamp: '2026-10-16T00:00:00Z', request_id: 'req_1', request, details}};
}

const notRunning = {resource: {kind: 'vm'}, requirement: 'running'};

for (const state of ['idle', 'running'] as const) {
  test(`a ${state} VM with a pending session and its policy rules decode`, async () => {
    const handle = await client(200, vm(state)).vm('vm_1').refresh();
    assert.equal(handle.state, state);
    assert.equal(handle.sessions?.[0]?.state, 'pending');
    assert.deepEqual(handle.policies?.rules, rules);
  });
}

test('sessions can be listed by the pending state', async () => {
  const calls: Call[] = [];
  const listed = await client(200, {sessions: [{session_id: 's1', state: 'pending', cwd: '/'}]}, calls)
    .vm('vm_1').listSessions({state: 'pending'});
  assert.equal(listed.sessions[0]?.state, 'pending');
  assert.match(calls[0]!.url, /\/sessions\?state=pending$/);
});

test('update sends rules and removals verbatim', async () => {
  const calls: Call[] = [];
  const policies: PolicyUpdate = {rules: {...rules, old: null}};
  await client(200, vm('idle'), calls).vm('vm_1').update({policies});
  assert.deepEqual(calls[0]!.body, {policies});
});

test('a policy read back updates without its read-only fields', async () => {
  const calls: Call[] = [];
  const read = {...vm('idle'), policies: {rules, secrets: {KEY: '***'}, mitm_domains: ['a'], warnings: ['w']}};
  const a = client(200, read, calls);
  const handle = await a.vm('vm_1').refresh();
  await handle.update({policies: handle.policies!});
  assert.deepEqual(calls[1]!.body, {policies: {rules, secrets: {KEY: '***'}}});
});

const notRunningCases: Array<[string, string, (a: Arker) => Promise<unknown>]> = [
  ['signal', 'createRun', (a) => a.vm('vm_1').signal('SIGINT')],
  ['fork', 'fork', (a) => a.vm('vm_1').fork()],
];
for (const [name, operationId, call] of notRunningCases) {
  test(`a ${name} on a VM that is not running is a typed invalid_state`, async () => {
    const calls: Call[] = [];
    const body = refusal('invalid_state', {kind: 'matched', operation_id: operationId}, notRunning);
    await assert.rejects(call(client(409, body, calls)), (error: unknown) => {
      assert.ok(error instanceof ArkerError);
      assert.equal(error.status, 409);
      assert.equal(error.code, 'invalid_state');
      assert.deepEqual(error.body, body.error);
      return true;
    });
    assert.equal(calls.length, 1);
  });
}

test('an inbound refusal keeps its typed invalid_state body', async () => {
  const body = refusal('invalid_state', {kind: 'unmatched', method: 'GET'}, notRunning);
  await assert.rejects(client(409, body).vm('vm_1').delete(), (error: unknown) =>
    error instanceof ArkerError && error.body?.code === 'invalid_state'
      && error.body.details.requirement === 'running');
});

test('a run on a PTY session is a typed resource_busy', async () => {
  const body = refusal('resource_busy', {kind: 'matched', operation_id: 'createRun'}, {resource: 'session'});
  await assert.rejects(client(409, body).vm('vm_1').run('ls', {session_id: 's1'}), (error: unknown) =>
    error instanceof ArkerError && error.status === 409 && error.body?.code === 'resource_busy'
      && error.body.details.resource === 'session');
});

test('a 202 background answer is running', async () => {
  const result = await client(202, {run_id: 'run_1', session_id: 's1', state: 'running'})
    .vm('vm_1').run('sleep 60', {time_to_background: 0});
  assert.deepEqual([result.type, result.runId, result.state], ['background', 'run_1', 'running']);
});
