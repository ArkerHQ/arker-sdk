import assert from 'node:assert/strict';
import {test} from 'node:test';
import {Arker, VM, type Vm} from '../src/index.js';
import {SandboxInstance} from '../src/blaxel.js';
import {blaxelCreationMetadata} from '../src/compat/blaxel-create.js';

function fixture(ttl: unknown = '300s', seconds = 300, fetch?: typeof globalThis.fetch) {
  const client = new Arker({apiKey:'test', baseUrl:'https://custom.example/api', controlBaseUrl:'https://control.example/api', fetch});
  const vm = new VM(client, 'vm-test', client.baseUrl, {vm_id:'vm-test', name:'source',
    metadata:{arker_blaxel:{version:1,labels:{},ttl}},
    policies:{rules:{'computesdk-blaxel-lifetime':{type:'schedule',action:'delete',after_created:seconds}}}} as unknown as Vm);
  const box = {provider:'arker',sandboxId:vm.id,getInstance:()=>vm};
  return Reflect.construct(SandboxInstance, [box, undefined, undefined, {process:{},fs:{},drives:{}}]) as SandboxInstance;
}

test('TTL readback preserves duration spelling and omits unknown or drifted values', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(fixture().spec)), {runtime:{ttl:'300s'}});
  assert.deepEqual(JSON.parse(JSON.stringify(fixture().spec)), {runtime:{ttl:'300s'}});
  for (const [ttl,seconds] of [[null,300],['5m',600],['bad',300]] as const)
    assert.equal(fixture(ttl,seconds).spec?.runtime, undefined);
  assert.equal((blaxelCreationMetadata({}, '5m') as {arker_blaxel:{ttl:string}}).arker_blaxel.ttl,'5m');
});

test('basic fork uses strict named endpoint and native response shape', async () => {
  const calls: {url:string; body:unknown; mode:string|null}[] = [];
  const box=fixture('5m',300, async (input,init) => {
    calls.push({url:String(input),body:JSON.parse(String(init?.body)),mode:new Headers(init?.headers).get('Arker-VM-Name-Mode')});
    return Response.json({vm_id:'vm-child',name:'child'});
  });
  assert.deepEqual(await box.fork('child'), {name:'child',type:'sandbox',snapshotId:''});
  assert.deepEqual(calls,[{url:'https://control.example/api/v1/named-fork',body:{source_vm_id:'vm-test',name:'child'},mode:'create'}]);
});

test('unsupported fork options reject before any request', async () => {
  const box=fixture('5m',300, async () => {assert.fail('unexpected mutation');});
  for(const options of [{targetType:'application'}, {snapshotId:'snapshot'}, {envs:[]}, {lifecycle:{}}])
    await assert.rejects(box.fork('child',options as never),/unsupported/i);
});


test('native fork delegates options and result identity', async () => {
  const expected = {name:'child',type:'application' as const,snapshotId:'snapshot'};
  const native = {metadata:{name:'source'},process:{},fs:{},fork:async (name:string, options:unknown) => {
    assert.equal(name,'child'); assert.deepEqual(options,{targetType:'application',port:8080}); return expected;
  }};
  const box=Reflect.construct(SandboxInstance,[{provider:'blaxel',sandboxId:'source'},native]) as SandboxInstance;
  assert.equal(await box.fork('child',{targetType:'application',port:8080}),expected);
});

test('fork collision uses native fork error shape with one strict request', async () => {
  let calls=0;
  const box=fixture('5m',300,async () => {
    calls++;
    return Response.json({error:{code:'conflict',message:'name exists',request_id:'test',timestamp:'2026-01-01T00:00:00Z',
      request:{kind:'matched',operation_id:'fork'},details:{resource:'vm'}}},{status:409});
  });
  await assert.rejects(box.fork('child'), error => {
    assert.equal((error as {code:number}).code,409);
    assert.equal((error as {error:string}).error,'name exists');
    return true;
  });
  assert.equal(calls,1);
});
