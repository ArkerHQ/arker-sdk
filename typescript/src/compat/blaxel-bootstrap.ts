import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type { VM } from '../index.js';
import { withStagedFirewallPackages } from './blaxel-packages.js';
import { guestScript as packagesScript, manifest as packagesManifest } from './blaxel-packages.generated.js';
import { stagePinnedArtifact } from './e2b-artifact.js';
import { guestScript as security } from './e2b-envd-guest.generated.js';
import { guestScript, manifest } from './blaxel-guest.generated.js';

const root = '/root/.arker/blaxel';
export async function bootstrapBlaxel(vm: VM, options: {
  configuration?: { envs: Record<string, string>; user: string }; cacheDirectory?: string;
} = {}) {
  const lifecycle = await vm._client._request<{customer_paused: boolean}>('GET', `/v1/vms/${vm.id}/lifecycle`, undefined, vm.baseUrl);
  if (lifecycle.customer_paused !== false) throw new Error('Native daemon bootstrap requires an explicitly running VM');
  const current = await vm.refresh();
  for (const rule of Object.values(current.policies?.rules ?? {})) {
    if (rule.type !== 'inbound' || rule.action === 'deny' || rule.auth === 'arker') continue;
    if (!rule.match?.ports || rule.match.ports.some(port => typeof port === 'number'
      ? port === 49983 : Math.min(...port) <= 49983 && Math.max(...port) >= 49983)) {
      throw new Error('Native daemon requires owner-authenticated ingress on port 49983');
    }
  }
  await vm.sync(`${root}/_guest_e2b_envd.py`, security, { isSecret: true });
  await vm.sync(`${root}/_guest_blaxel.py`, guestScript, { isSecret: true });
  await vm.sync(`${root}/_guest_blaxel_packages.py`, packagesScript, { isSecret: true });
  await vm.sync(`${root}/_guest_blaxel_packages.json`, JSON.stringify(packagesManifest), { isSecret: true });
  await vm.sync(`${root}/manifest.json`, JSON.stringify(manifest), { isSecret: true });
  await vm.sync(`${root}/LICENSE`, manifest.license, { isSecret: true });
  async function invoke(operation: string, argument = '') {
    const result = await vm.run(`python3 ${root}/_guest_blaxel.py ${operation} ${argument}`,
      { timeout: 120, end_after_all_subprocesses: false });
    if (result.exitCode !== 0) throw new Error('Native daemon bootstrap failed: ' + JSON.parse(result.stdout).error);
    return JSON.parse(result.stdout);
  }
  const prepared = await invoke('prepare');
  if (prepared.arch !== 'amd64' && prepared.arch !== 'arm64') throw new Error('Unsupported daemon architecture');
  let candidate: string | null = null;
  const request = `${root}/requests/${randomUUID()}.json`;
  let failure: unknown;
  let failed = false;
  try {
    if (prepared.verified !== true) candidate = await stagePinnedArtifact(vm, root,
      manifest.artifacts[prepared.arch as 'amd64' | 'arm64'], options.cacheDirectory ?? join(homedir(), '.cache/arker/blaxel'));
    const ensure = async (candidates: string[]) => {
      await vm.sync(request, JSON.stringify({ vm_id: vm.id, candidate, configuration: options.configuration,
        firewall_profile: prepared.firewall_profile, package_candidates: candidates }), { isSecret: true });
      const ready = await invoke('ensure', request);
      if (ready.ready !== true || ready.version !== manifest.version) throw new Error('Native daemon did not establish readiness');
      return ready;
    };
    return prepared.firewall_verified === false
      ? await withStagedFirewallPackages(vm, prepared.firewall_profile, options.cacheDirectory ?? join(homedir(), '.cache/arker/blaxel'), ensure)
      : await ensure([]);
  } catch (error) {
    failed = true;
    failure = error;
    throw error;
  } finally {
    try {
      const cleanup = await vm.run(`rm -f -- ${request}${candidate ? ' ' + candidate : ''}`, { timeout: 10 });
      if (cleanup.exitCode !== 0) throw new Error('Native daemon staging cleanup failed');
    } catch (error) {
      if (failed) throw new AggregateError([failure, error], "Native daemon bootstrap and staging cleanup failed");
      throw error;
    }
  }
}
