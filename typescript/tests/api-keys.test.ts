// API keys: create, list and revoke on the control plane, with scopes as
// `{vm_id}` or `{pool_id}` objects.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Arker, ArkerError, type ApiKeyScope, type CreateApiKeyRequest } from '../src/index';

const poolId = '0b6c1d2e-3f40-4a51-8b62-7c83d94ea5f6';
const createdAt = '2026-10-17T00:00:00.000Z';

// Compile-time: only the object scopes exist; the withdrawn fields are gone.
// @ts-expect-error scopes are objects, not "vmid:<id>" strings
const noStringScope: ApiKeyScope = 'vmid:vm_a';
// @ts-expect-error a key has no role of its own
const noRole: CreateApiKeyRequest = {role: 'viewer'};
// @ts-expect-error there is no action-limited key
const noAction: CreateApiKeyRequest = {pool_id: poolId, action: 'join'};
void [noStringScope, noRole, noAction];

type Call = {method: string; url: string; body?: unknown};

function client(status: number, body: unknown, calls: Call[] = []): Arker {
  return new Arker({apiKey: 'k', baseUrl: 'https://region.invalid', controlBaseUrl: 'https://control.invalid',
    retry: false,
    fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({method: init?.method ?? 'GET', url: String(input),
        body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined});
      return body === undefined ? new Response(null, {status}) : Response.json(body, {status});
    }) as typeof fetch});
}

const created = (scopes: ApiKeyScope[]) =>
  ({api_key_id: 'agents_abc', scopes, created_at: createdAt, key: 'ark_live_secret'});

for (const request of [
  {name: 'agents', scopes: [{pool_id: poolId}]},
  {name: 'agent', scopes: [{vm_id: 'vm_a'}, {vm_id: 'vm_b'}]},
  {scopes: []},
] satisfies CreateApiKeyRequest[]) {
  test(`createApiKey sends ${JSON.stringify(request)} to the control plane`, async () => {
    const calls: Call[] = [];
    const key = await client(201, created(request.scopes), calls).createApiKey(request);
    assert.deepEqual(calls, [{method: 'POST', url: 'https://control.invalid/v1/api-keys', body: request}]);
    assert.equal(key.key, 'ark_live_secret');
    assert.equal(key.api_key_id, 'agents_abc');
    assert.equal(key.created_at, createdAt);
    assert.deepEqual(key.scopes, request.scopes);
  });
}

test('createApiKey with no request is an organization key', async () => {
  const calls: Call[] = [];
  await client(201, created([]), calls).createApiKey();
  assert.deepEqual(calls[0]?.body, {});
});

test('listApiKeys decodes each key', async () => {
  const calls: Call[] = [];
  const api_keys = [
    {api_key_id: 'org_1', scopes: [], created_at: createdAt},
    {api_key_id: 'pool_1', scopes: [{pool_id: poolId}], created_at: createdAt},
  ];
  const listed = await client(200, {api_keys}, calls).listApiKeys();
  assert.deepEqual(calls, [{method: 'GET', url: 'https://control.invalid/v1/api-keys', body: undefined}]);
  assert.deepEqual(listed.api_keys, api_keys);
});

test('revokeApiKey deletes the escaped id and resolves to undefined', async () => {
  const calls: Call[] = [];
  assert.equal(await client(204, undefined, calls).revokeApiKey('agents/abc'), undefined);
  assert.deepEqual(calls, [{method: 'DELETE', url: 'https://control.invalid/v1/api-keys/agents%2Fabc', body: undefined}]);
});

const scopesShape = 'scopes must be 1..16 {"vm_id"} objects or one {"pool_id"}';

function refusal(code: string, details?: object) {
  return {error: {code, message: 'refused', timestamp: '2026-10-17T00:00:00Z', request_id: 'req_1',
    request: {kind: 'matched', operation_id: 'createApiKey'}, ...(details ? {details} : {})}};
}

test('bad scopes reject with a typed bad_request and are not retried', async () => {
  const calls: Call[] = [];
  const request = {scopes: [{pool_id: poolId}, {vm_id: 'vm_a'}]};
  const bad = refusal('bad_request', {violations: [{field: 'request', message: scopesShape}]});
  await assert.rejects(client(400, bad, calls).createApiKey(request), (error: unknown) =>
    error instanceof ArkerError && error.status === 400 && error.code === 'bad_request'
    && error.body?.code === 'bad_request' && error.body.details?.violations?.[0]?.message === scopesShape);
  assert.equal(calls.length, 1);
});

for (const [status, code, details] of [[403, 'forbidden', undefined], [404, 'not_found', {resource: 'pool'}]] as const) {
  test(`a ${status} ${code} refusal keeps its code`, async () => {
    await assert.rejects(client(status, refusal(code, details)).createApiKey({scopes: [{pool_id: poolId}]}),
      (error: unknown) => error instanceof ArkerError && error.status === status && error.code === code);
  });
}
