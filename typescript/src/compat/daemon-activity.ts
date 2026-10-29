import type { VM } from '../index.js';

type Ready = { ready: true; version: string; activity: { pid: number; start_ticks: number; boot_id: string } };
const sleep = () => new Promise(resolve => setTimeout(resolve, 50));

/** Retain the bootstrap run until its verified daemon is registered. */
export async function registeredBootstrap(vm: VM, command: string, request: string, version: string): Promise<Ready> {
  const run = await vm.run(command, { timeout: 120, time_to_background: 0, end_after_all_subprocesses: false });
  if (run.type !== 'background') throw new Error('Daemon bootstrap did not retain its activity guard');
  const deadline = Date.now() + 120_000;
  const acknowledgement = request.replace(/\.[^.]+$/, '.registered');
  let acknowledged = false;
  let failure: unknown;
  let failed = false;
  try {
    let ready: Ready | undefined;
    while (Date.now() < deadline) {
      const current = await vm.getRun(run.runId);
      const output = new TextDecoder().decode(current.stdoutBytes);
      if (output.includes('\n')) {
        if (current.state !== 'running') throw new Error('Daemon bootstrap released its activity guard before registration');
        const value = JSON.parse(output.split("\n")[0]!);
        const identity = value.activity;
        if (value.ready !== true || value.version !== version) throw new Error('Native daemon did not establish readiness');
        if (!identity || !Number.isSafeInteger(identity.pid) || identity.pid < 2 ||
            !Number.isSafeInteger(identity.start_ticks) || identity.start_ticks < 0 ||
            typeof identity.boot_id !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(identity.boot_id)) {
          throw new Error('Invalid daemon activity identity');
        }
        await vm._client._request('POST', `/v1/vms/${vm.id}/daemon-activity`,
          {pid: identity.pid, start_ticks: identity.start_ticks, boot_id: identity.boot_id}, vm.baseUrl,
          undefined, undefined, undefined, {signal: AbortSignal.timeout(60_000)});
        await vm.sync(acknowledgement, '{"registered":true}', {isSecret: true});
        acknowledged = true;
        ready = value;
        break;
      }
      if (!['running', 'pending'].includes(current.state)) throw new Error('Daemon bootstrap ended before registration');
      await sleep();
    }
    if (!ready) throw new Error('Daemon bootstrap identity timed out');
    while (Date.now() < deadline) {
      const current = await vm.getRun(run.runId);
      if (!['running', 'pending'].includes(current.state)) {
        if (current.state !== 'completed' || current.exit_code !== 0) throw new Error('Daemon bootstrap failed after registration');
        return ready;
      }
      await sleep();
    }
    throw new Error('Daemon bootstrap acknowledgement timed out');
  } catch (error) {
    failure = error;
    failed = true;
    throw error;
  } finally {
    if (!acknowledged) {
      try { await vm.sync(acknowledgement, '{"registered":false}', {isSecret: true}); }
      catch (error) {
        if (failed) throw new AggregateError([failure, error], 'Daemon registration and release failed');
        throw error;
      }
    }
  }
}
