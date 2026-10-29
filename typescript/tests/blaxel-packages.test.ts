import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import type { VM } from '../src/index.js';
import { withStagedFirewallPackages } from '../src/compat/blaxel-packages.js';
import { manifest } from '../src/compat/blaxel-packages.generated.js';

test('package staging cleans both filenames and retains installation plus cleanup failures', async () => {
  const cache = await mkdtemp(join(tmpdir(), 'blaxel-packages-'));
  const content = Buffer.from('signed package fixture');
  const digest = createHash('sha256').update(content).digest('hex');
  const packages = manifest.packages as unknown as Array<{url: string; sha256: string}>;
  const original = [...packages];
  packages.splice(0, packages.length, {url: 'https://dl-cdn.alpinelinux.org/package.apk', sha256: digest});
  try {
    await writeFile(join(cache, digest), content);
    for (const failure of [undefined, 'rename', 'install', 'cleanup', 'both']) {
      const calls: string[] = [];
      const uploaded: string[] = [];
      const primary = new Error('installation failed'), cleanup = new Error('cleanup failed');
      const vm = {
        sync: async (path: string) => { uploaded.push(path); },
        run: async (command: string) => {
          calls.push(command);
          if (command.startsWith('/bin/mv') && failure === 'rename') throw primary;
          if (command.startsWith('/bin/rm') && ['cleanup', 'both'].includes(failure!)) throw cleanup;
          return {exitCode: 0};
        },
      } as unknown as VM;
      const operation = withStagedFirewallPackages(vm, manifest.profile, cache, async candidates => {
        assert.deepEqual(candidates, uploaded.map(path => path + '.apk'));
        if (['install', 'both'].includes(failure!)) throw primary;
        return 42;
      });
      if (!failure) assert.equal(await operation, 42);
      else await assert.rejects(operation, error => {
        if (failure === 'both') {
          assert.ok(error instanceof AggregateError);
          assert.deepEqual(error.errors, [primary, cleanup]);
        } else assert.equal(error, failure === 'cleanup' ? cleanup : primary);
        return true;
      });
      const last = calls.at(-1)!;
      assert.ok(last.startsWith('/bin/rm -f -- '));
      assert.ok(last.includes("'" + uploaded[0] + "'"));
      assert.ok(last.includes("'" + uploaded[0] + ".apk'"));
      assert.ok(!last.includes('*'));
    }
  } finally {
    packages.splice(0, packages.length, ...original);
    await rm(cache, {recursive: true});
  }
});

test("unqualified image fails before transport", async () => {
  await assert.rejects(withStagedFirewallPackages({} as VM, "blaxel/base-image:latest", "/unused",
    async () => assert.fail("must not install")), /runtime profile/);
});

test('bootstrap preserves cached qualification through ensure and refuses public ingress first', async () => {
  const {bootstrapBlaxel} = await import('../src/compat/blaxel-bootstrap.js');
  const {manifest: daemon} = await import('../src/compat/blaxel-guest.generated.js');
  const writes = new Map<string, string>();
  const commands: string[] = [];
  let publicIngress = false;
  let acknowledged = false;
  const vm = {
    id: 'test', baseUrl: 'https://custom.example/api',
    _client: {_request: async (method: string) => method === 'GET' ? {customer_paused: false} : undefined},
    refresh: async () => ({policies: {rules: publicIngress
      ? {public: {type: 'inbound', action: 'allow', auth: 'open', match: {ports: [49983]}}} : {}}}),
    sync: async (path: string, value: string) => { writes.set(path, value); if (path.endsWith('.registered')) acknowledged = true; },
    getRun: async () => ({state: acknowledged ? 'completed' : 'running', exit_code: 0,
      stdoutBytes: new TextEncoder().encode(JSON.stringify({ready: true, version: daemon.version,
        activity: {pid: 17, start_ticks: 1234, boot_id: '00000000-0000-0000-0000-000000000001'}})+'\n')}),
    run: async (command: string) => {
      commands.push(command);
      if (command.includes(' prepare')) return {exitCode: 0, stdout: JSON.stringify({arch: 'amd64', verified: true,
        firewall_profile: manifest.profile, firewall_verified: true})};
      if (command.includes(' ensure')) {
        const request = JSON.parse(writes.get(command.split(' ').at(-1)!)!);
        assert.equal(request.firewall_profile, manifest.profile);
        assert.deepEqual(request.package_candidates, []);
        return {type: "background", runId: "run"};
      }
      return {exitCode: 0, stdout: ''};
    },
  } as unknown as VM;
  await bootstrapBlaxel(vm);
  assert.ok(writes.has('/root/.arker/blaxel/_guest_blaxel_packages.json'));
  assert.equal(commands.length, 3);
  publicIngress = true; writes.clear(); commands.length = 0;
  await assert.rejects(bootstrapBlaxel(vm), /owner-authenticated/);
  assert.equal(writes.size, 0);
  assert.deepEqual(commands, []);
});

test('qualified image cannot start its entrypoint before firewall installation', async () => {
  const {validateFirewallStartup} = await import('../src/compat/blaxel-packages.js');
  for (const config of [undefined, {}, {entrypoint: []}, {cmd: []}, {entrypoint: ['daemon'], cmd: []}]) {
    assert.throws(() => validateFirewallStartup(manifest.image, config), /empty entrypoint and cmd/);
    validateFirewallStartup('other-explicit-image', config);
  }
  validateFirewallStartup(manifest.image, {entrypoint: [], cmd: [], env: {HOME: '/blaxel'}});
});
