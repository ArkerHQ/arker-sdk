/** Keep native daemon work runnable until explicit pause, expiry, or daemon exit.
 * This intentionally keeps an otherwise idle sandbox active. A reconnect never
 * cancels the shared lifetime run, even if its own readiness request fails.
 */
import {createHash} from 'node:crypto';
import type {VM} from '../index.js';
import {guestJson, scriptPath, quote} from './e2b-bootstrap.js';

type Lifetime = {
  incarnation: {vm_id: string; pid: number; start: string; boot_id: string};
  session_id: string | null;
};
export async function retainEnvd(vm: VM): Promise<string> {
  const inspect = (values = {}) => guestJson(vm, 'lifetime', JSON.stringify({vm_id: vm.id, ...values})) as Promise<Lifetime>;
  let current = await inspect();
  if (current.session_id === null) {
    const candidate = (await vm.createSession()).session_id;
    // An ambiguous claim may have won: do not delete it without an answer.
    current = await inspect({incarnation: current.incarnation, session_id: candidate});
    if (current.session_id !== candidate) await vm.deleteSession(candidate);
  }
  const identity = current.incarnation;
  if (identity.vm_id !== vm.id || !current.session_id) throw new Error('Invalid envd lifetime identity');
  const argument = JSON.stringify([vm.id, identity.pid, identity.start, identity.boot_id, current.session_id]);
  const run = await vm.run(`python3 ${quote(scriptPath)} monitor ${quote(argument)}`, {
    session_id: current.session_id, timeout: 0, time_to_background: 0, end_after_all_subprocesses: false,
    idempotencyKey: createHash('sha256').update(argument).digest('hex'),
  });
  if (run.type !== 'background') throw new Error('Envd lifetime run is not active');
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const status = await vm.getRun(run.runId);
    if (status.vm_id !== vm.id || status.session_id !== current.session_id) throw new Error('Envd lifetime run belongs to a different VM or session');
    if (!['pending', 'running'].includes(status.state)) throw new Error('Envd lifetime run ended before readiness');
    const output = new TextDecoder().decode(status.stdoutBytes);
    if (output.includes('\n')) {
      const ready = JSON.parse(output.split('\n')[0]!);
      const held = ready.lifetime;
      if (status.state !== 'running' || ready.ready !== true || held?.session_id !== current.session_id ||
          JSON.stringify([held?.incarnation?.vm_id, held?.incarnation?.pid, held?.incarnation?.start,
            held?.incarnation?.boot_id, held?.session_id]) !== argument) {
        throw new Error('Envd lifetime acknowledgement does not match');
      }
      if ((await guestJson(vm, "publish", argument)).ready !== true) throw new Error("Envd lifetime ingress was not released");
      return run.runId;
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('Envd lifetime readiness timed out');
}
