import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import test from 'node:test';
import {retainEnvd} from '../src/compat/e2b-lifetime.js';

function fixture() {
  const held = {incarnation: {vm_id:'vm-a', pid:123, start:'456', boot_id:'boot'}, session_id:'owned'};
  const calls: Array<{command:string; options:Record<string, unknown>}> = [];
  const deleted: string[] = [];
  const vm = {
    id:'vm-a', state:'running', held, calls, deleted, published:0,
    createSession: async () => ({session_id:'candidate'}),
    deleteSession: async (id:string) => {deleted.push(id);},
    run: async (command:string, options:Record<string, unknown>) => {
      if (command.includes(' publish ')) {vm.published++; return {exitCode:0, stdout:'{"ready":true}'};}
      if (command.includes(' lifetime ')) return {exitCode:0, stdout:JSON.stringify(vm.held)};
      calls.push({command,options});
      return {type:'background', runId:'shared-run'};
    },
    getRun: async () => ({vm_id:vm.id, session_id:vm.held.session_id, state:vm.state,
      stdoutBytes:Buffer.from(JSON.stringify({ready:true,lifetime:vm.held})+'\n')}),
  };
  return vm;
}

test('lifetime reconnect uses the same cross-language key and dedicated unbounded run', async () => {
  const vm = fixture();
  assert.equal(await retainEnvd(vm as never), 'shared-run');
  assert.equal(await retainEnvd(vm as never), 'shared-run');
  assert.deepEqual(vm.calls[0], vm.calls[1]);
  const argument = '["vm-a",123,"456","boot","owned"]';
  assert.ok(vm.calls[0]!.command.includes(argument));
  assert.deepEqual(vm.calls[0]!.options, {session_id:'owned', timeout:0, time_to_background:0,
    end_after_all_subprocesses:false, idempotencyKey:createHash('sha256').update(argument).digest('hex')});
  assert.deepEqual(vm.deleted, []);
});
for (const state of ['completed','failed','cancelled']) test(`stale ready cannot acknowledge ${state}`, async () => {
  const vm = fixture(); vm.state = state;
  await assert.rejects(retainEnvd(vm as never), /ended before readiness/);
  assert.deepEqual(vm.deleted, []);
  assert.equal(vm.published, 0);
});
test('wrong VM acknowledgement is rejected without cancelling a shared run', async () => {
  const vm = fixture(); const original = vm.getRun;
  vm.getRun = async () => ({...await original(), vm_id:'parent'});
  await assert.rejects(retainEnvd(vm as never), /different VM/);
  assert.equal(vm.published, 0);
  assert.deepEqual(vm.deleted, []);
});
test('fork obtains child-bound host authority', async () => {
  const vm = fixture(); await retainEnvd(vm as never);
  vm.id = 'child'; vm.held = {...vm.held, incarnation:{...vm.held.incarnation,vm_id:'child'},session_id:'child-session'};
  await retainEnvd(vm as never);
  assert.notEqual(vm.calls[0]!.options.idempotencyKey, vm.calls[1]!.options.idempotencyKey);
});
test('lost response reconnect repeats the identical run without deleting uncertain ownership', async () => {
  const vm = fixture(), run = vm.run;
  vm.run = async (command, options) => {
    const result = await run(command, options);
    if (command.includes(' monitor ')) throw new Error('response lost');
    return result;
  };
  await assert.rejects(retainEnvd(vm as never), /response lost/);
  vm.run = run;
  await retainEnvd(vm as never);
  assert.deepEqual(vm.calls[0], vm.calls[1]);
  assert.deepEqual(vm.deleted, []);
});
