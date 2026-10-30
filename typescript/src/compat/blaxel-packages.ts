import type { VM } from '../index.js';
import { stagePinnedArtifact } from './e2b-artifact.js';
import { manifest } from './blaxel-packages.generated.js';

const root = '/root/.arker/blaxel';
const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";

/** Scope ownership of every package candidate, including partially renamed uploads. */
export async function withStagedFirewallPackages<T>(
  vm: VM, profile: string, cacheDirectory: string, install: (candidates: string[]) => Promise<T>,
): Promise<T> {
  if (profile !== manifest.profile) throw new Error('Firewall packages require the supported runtime profile');
  const owned: string[] = [];
  const candidates: string[] = [];
  let failure: unknown;
  let failed = false;
  try {
    for (const entry of manifest.packages) {
      const original = await stagePinnedArtifact(vm, root, entry, cacheDirectory);
      const destination = original + '.apk';
      owned.push(original, destination);
      const result = await vm.run(`/bin/mv -- ${quote(original)} ${quote(destination)}`, { timeout: 10 });
      if (result.exitCode !== 0) throw new Error('Firewall package staging rename failed');
      candidates.push(destination);
    }
    return await install(candidates);
  } catch (error) {
    failed = true;
    failure = error;
    throw error;
  } finally {
    if (owned.length) {
      try {
        const result = await vm.run(`/bin/rm -f -- ${owned.map(quote).join(' ')}`, { timeout: 10 });
        if (result.exitCode !== 0) throw new Error('Firewall package staging cleanup failed');
      } catch (error) {
        if (failed) throw new AggregateError([failure, error], 'Firewall package installation and cleanup failed');
        throw error;
      }
    }
  }
}

export function validateFirewallStartup(image: unknown, configuration: unknown): void {
  if (image !== manifest.image) return;
  const value = configuration as {entrypoint?: unknown; cmd?: unknown} | undefined;
  if (!value || !Array.isArray(value.entrypoint) || value.entrypoint.length !== 0 ||
      !Array.isArray(value.cmd) || value.cmd.length !== 0) {
    throw new Error('Qualified firewall image requires explicit empty entrypoint and cmd before creation');
  }
}
