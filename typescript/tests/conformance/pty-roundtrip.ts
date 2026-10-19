/**
 * PTY round-trip e2e - the interactive-terminal path that `arker shell`
 * drives under the hood (SDK `vm.connectPty`). Complements the
 * unit tests (which fake the socket) by exercising a REAL PTY-over-WebSocket
 * against a live backend: open → receive output → send keystrokes → resize →
 * reconnect-same-shell (persist) → kill→fresh.
 *
 *   bun run build   # produces dist/ (connectPty needs the optional `ws` dep)
 *   ARKER_API_KEY=ark_live_... \
 *   ARKER_BASE_URL=http://<worker>:8080/api  (or ARKER_REGION=us-west-2) \
 *   ARKER_SOURCE_VM=<source-name> ARKER_SOURCE_ORG_ID=<source-org> \
 *   bun tests/conformance/pty-roundtrip.ts
 *
 * Exits non-zero on any failure. Self-cleans the VM it forks.
 */
import { Arker, ARKER_ORG_ID, type PtyConnection } from "../../src/index.js";

const API_KEY = process.env.ARKER_API_KEY;
const BASE_URL = process.env.ARKER_BASE_URL;
const REGION = process.env.ARKER_REGION;
const SOURCE = process.env.ARKER_SOURCE_VM;
const SOURCE_ORG_ID = process.env.ARKER_SOURCE_ORG_ID ?? ARKER_ORG_ID;
if (!API_KEY) { console.error("FATAL: set ARKER_API_KEY"); process.exit(2); }
if (!BASE_URL && !REGION) { console.error("FATAL: set ARKER_BASE_URL or ARKER_REGION"); process.exit(2); }
if (!SOURCE) { console.error("FATAL: set ARKER_SOURCE_VM"); process.exit(2); }

const arker = new Arker({ apiKey: API_KEY, baseUrl: BASE_URL, region: REGION });
const dec = new TextDecoder();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

let pass = 0, fail = 0;
function check(label: string, ok: boolean, detail = ""): void {
  if (ok) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}  ${detail.slice(0, 160).replace(/\n/g, "⏎")}`); }
}
/** Reconnecting to a persisted session must return the same shell promptly. */

/** Attach a buffer to a PTY; returns helpers to drive it like a terminal. */
function driver(pty: PtyConnection) {
  let buf = "";
  pty.onData((b) => { buf += dec.decode(b); });
  return {
    buf: () => buf,
    clear: () => { buf = ""; },
    type: (s: string) => pty.send(s),
    /** Send `cmd` (if given) and wait until `token` shows in output (or timeout). */
    async expect(token: string, cmd?: string, tries = 20): Promise<boolean> {
      for (let i = 0; i < tries; i++) {
        if (cmd && i === 0) pty.send(cmd);
        await sleep(600);
        if (buf.includes(token)) return true;
      }
      return false;
    },
  };
}

/** Open a PTY on `sessionId`, wait until the shell echoes (is live).
 *  Reconnecting to a paused/suspended VM, the host restores the runtime during
 *  the WS attach, so a fresh attempt may need a moment to settle. We retry the
 *  whole connectPty (a real client reopens), let each attempt settle, then
 *  probe patiently on it before giving up. */
async function live(vmId: string, sessionId: string, opts: Record<string, unknown> = {}): Promise<{ pty: PtyConnection; d: ReturnType<typeof driver> } | null> {
  for (let attempt = 0; attempt < 8; attempt++) {
    let pty: PtyConnection;
    try {
      pty = await arker.vm(vmId).connectPty({ sessionId, cols: 80, rows: 24, persist: true, ...opts });
    } catch {
      await sleep(2000);
      continue;
    }
    let closedEarly = false;
    pty.onClose(() => { closedEarly = true; });
    await pty.ready.catch(() => { closedEarly = true; });
    if (closedEarly) { try { pty.close(); } catch { /* ignore */ } await sleep(2000); continue; }
    await sleep(1500); // let the reattach/scrollback-replay settle
    const d = driver(pty);
    for (let i = 0; i < 12 && !closedEarly; i++) {
      d.clear();
      if (await d.expect("LIVE_OK", "echo LIVE_OK\n", 1)) return { pty, d };
    }
    try { pty.close(); } catch { /* ignore */ }
    await sleep(2000);
  }
  return null;
}

async function main(): Promise<void> {
  console.log(`==== PTY round-trip e2e vs ${BASE_URL ?? REGION} (source=${SOURCE}) ====`);
  const vm = await arker.fork({ source_vm_name: SOURCE!, source_org_id: SOURCE_ORG_ID, name: "pty-e2e" });
  const vmId = vm.id;
  console.log(`  (vm=${vmId})`);
  try {
    // warm the VM so the first PTY attach doesn't race a cold start
    await arker.vm(vmId).run("echo warmup", { timeout: 60 }).catch(() => {});
    const session = await arker.vm(vmId).createSession();
    const sid = session.session_id ?? (session as { id?: string }).id!;

    // 1) open + I/O round-trip
    const a = await live(vmId, sid);
    check("connect + shell is live", a !== null);
    if (a) {
      a.d.clear();
      check("keystroke → command output", await a.d.expect("RT=hello", "echo RT=hello\n"));
      // 2) resize → the VM's terminal reflects the new size
      a.pty.resize(120, 40);
      a.d.clear();
      check("resize → stty size = 40 120", await a.d.expect("40 120", "stty size\n"));
      // seed a shell var, then detach (persist) for the reconnect test
      a.d.clear();
      await a.d.expect("SEED=ZZ", "export MARK=ZZ; echo SEED=$MARK\n");
      a.pty.close();
    }
    await sleep(4000);

    // 3) reconnect to the SAME session → same shell ($MARK survives)
    const b = await live(vmId, sid);
    check("reconnect same session", b !== null);
    if (b) {
      b.d.clear();
      check("reconnect = same shell ($MARK survives)", await b.d.expect("MARK=[ZZ]", "echo MARK=[$MARK]\n"));
      b.pty.kill();
      await sleep(500);
      b.pty.close();
    }
    await sleep(4000);

    const c = await live(vmId, sid);
    check("reconnect after kill", c !== null);
    if (c) {
      c.d.clear();
      check("kill → fresh shell ($MARK gone)", await c.d.expect("MARK=[]", "echo MARK=[$MARK]\n"));
      c.pty.close();
    }
  } finally {
    await arker.vm(vmId).delete().catch(() => {});
  }

  console.log(`\n==== PTY e2e: ${pass} passed, ${fail} failed ====`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error("PTY e2e crashed:", e); process.exit(1); });
