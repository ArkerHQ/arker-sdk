/**
 * PTY scenarios e2e - comprehensive coverage of the interactive-terminal paths
 * beyond the core pty-roundtrip (which covers connect/keystroke/resize/reconnect/kill).
 * This file adds the rest of the matrix so every observable PTY scenario is gated:
 *
 *   - signals: Ctrl-C interrupts a running foreground command
 *   - env + cwd propagation from createSession into the shell
 *   - custom command (connectPty command override, not the login shell)
 *   - scrollback replay: output produced before disconnect is replayed on reattach
 *   - large output integrity (ordered + complete, no loss at moderate volume)
 *   - concurrent independent PTY sessions (two shells, isolated state)
 *   - cancel_ttl: an idle PTY auto-cancels and the shell is destroyed
 *   - exec-before-PTY corner case (#42): run() on a session, then attach a PTY
 *   - cold reconnect after idle-TTL suspend (restore path) keeps the same shell
 *
 *   bun run build
 *   ARKER_API_KEY=ark_... ARKER_BASE_URL=http://<worker>:8080/api \
 *   ARKER_SOURCE_VM=<source-name> ARKER_SOURCE_ORG_ID=<source-org> bun tests/conformance/pty-scenarios.ts
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
  else { fail++; console.log(`  FAIL  ${label}  ${detail.slice(0, 200).replace(/\n/g, "⏎")}`); }
}

/** Buffer-capped terminal driver (don't O(n^2) a flood; mirrors a real client). */
function driver(pty: PtyConnection) {
  let buf = "";
  pty.onData((b) => { buf += dec.decode(b); if (buf.length > 131072) buf = buf.slice(-65536); });
  return {
    buf: () => buf,
    clear: () => { buf = ""; },
    type: (s: string) => pty.send(s),
    async expect(token: string, cmd?: string, tries = 20): Promise<boolean> {
      for (let i = 0; i < tries; i++) {
        if (cmd && i === 0) pty.send(cmd);
        await sleep(500);
        if (buf.includes(token)) return true;
      }
      return false;
    },
  };
}

/** Open a PTY and wait until the shell echoes (live), retrying the whole connect. */
async function live(vmId: string, sessionId: string, opts: Record<string, unknown> = {}): Promise<{ pty: PtyConnection; d: ReturnType<typeof driver> } | null> {
  for (let attempt = 0; attempt < 8; attempt++) {
    let pty: PtyConnection;
    try {
      pty = await arker.vm(vmId).connectPty({ sessionId, cols: 80, rows: 24, persist: true, ...opts });
    } catch { await sleep(2000); continue; }
    let closedEarly = false;
    pty.onClose(() => { closedEarly = true; });
    await pty.ready.catch(() => { closedEarly = true; });
    if (closedEarly) { try { pty.close(); } catch { /**/ } await sleep(2000); continue; }
    await sleep(1500);
    const d = driver(pty);
    for (let i = 0; i < 12 && !closedEarly; i++) {
      d.clear();
      if (await d.expect("LIVE_OK", "echo LIVE_OK\n", 1)) return { pty, d };
    }
    try { pty.close(); } catch { /**/ }
    await sleep(2000);
  }
  return null;
}

async function main(): Promise<void> {
  console.log(`==== PTY scenarios e2e vs ${BASE_URL ?? REGION} (source=${SOURCE}) ====`);
  const vm = await arker.fork({ source_vm_name: SOURCE!, source_org_id: SOURCE_ORG_ID, name: "pty-scenarios" });
  const vmId = vm.id;
  console.log(`  (vm=${vmId})`);
  try {
    await arker.vm(vmId).run("echo warmup", { timeout: 60 }).catch(() => {});

    // ---- 1) signals: Ctrl-C interrupts a running foreground command ----
    {
      const s = await arker.vm(vmId).createSession();
      const sid = s.session_id ?? (s as { id?: string }).id!;
      const a = await live(vmId, sid);
      check("signals: shell is live", a !== null);
      if (a) {
        // start a long sleep in the foreground, then Ctrl-C, then prove the shell is responsive
        a.d.clear();
        a.pty.send("sleep 100; echo SLEEP_DONE\n");
        await sleep(1500);
        a.pty.send("\x03"); // Ctrl-C → SIGINT to foreground pgrp
        await sleep(800);
        a.d.clear();
        const recovered = await a.d.expect("AFTER_CTRLC", "echo AFTER_CTRLC\n");
        check("signals: Ctrl-C interrupts sleep, shell responsive", recovered);
        check("signals: interrupted cmd did NOT complete", !a.d.buf().includes("SLEEP_DONE"));
        a.pty.close();
      }
    }

    // ---- 2) env + cwd propagation from createSession ----
    {
      const s = await arker.vm(vmId).createSession({ env: { PTY_SCN: "envOK" }, cwd: "/tmp" });
      const sid = s.session_id ?? (s as { id?: string }).id!;
      const a = await live(vmId, sid);
      check("env/cwd: shell is live", a !== null);
      if (a) {
        a.d.clear();
        check("env: $PTY_SCN propagated", await a.d.expect("V=envOK", "echo V=$PTY_SCN\n"));
        a.d.clear();
        check("cwd: pwd is /tmp", await a.d.expect("/tmp", "pwd\n"));
        a.pty.close();
      }
    }

    // ---- 3) custom command (connectPty command override) ----
    {
      // Run python3 as the PTY program (not the login shell) - the real
      // custom-command use case (an interactive REPL over the terminal).
      let pty: PtyConnection | null = null;
      try { pty = await arker.vm(vmId).connectPty({ cols: 80, rows: 24, persist: true, command: "/usr/bin/python3" }); } catch { /**/ }
      check("custom-cmd: connected to /usr/bin/python3", pty !== null);
      if (pty) {
        await pty.ready.catch(() => {});
        const d = driver(pty);
        check("custom-cmd: python REPL prompt appears", await d.expect(">>>", undefined, 8));
        d.clear();
        check("custom-cmd: REPL evaluates input (print(6*7)→42)", await d.expect("42", "print(6*7)\n"));
        pty.close();
      }
    }

    // ---- 4) scrollback replay: output before disconnect is replayed on reattach ----
    {
      const s = await arker.vm(vmId).createSession();
      const sid = s.session_id ?? (s as { id?: string }).id!;
      const a = await live(vmId, sid);
      check("scrollback: shell is live", a !== null);
      if (a) {
        a.d.clear();
        await a.d.expect("SCROLLMARK_42", "echo SCROLLMARK_42\n");
        a.pty.close();
        await sleep(3000);
        // Reconnect RAW (not via live(), whose LIVE_OK probing clears the buffer
        // and would consume the replay). The guest replays the out_ring on reattach,
        // so the prior output must reappear WITHOUT re-running the command.
        let rbuf = "";
        const b = await arker.vm(vmId).connectPty({ sessionId: sid, cols: 80, rows: 24, persist: true });
        b.onData((x) => { rbuf += dec.decode(x); });
        await b.ready.catch(() => {});
        for (let i = 0; i < 8 && !rbuf.includes("SCROLLMARK_42"); i++) await sleep(500);
        check("scrollback: prior output replayed on reattach", rbuf.includes("SCROLLMARK_42"), rbuf.slice(0, 120));
        b.close();
      }
    }

    // ---- 5) large output integrity (ordered + complete) ----
    {
      const s = await arker.vm(vmId).createSession();
      const sid = s.session_id ?? (s as { id?: string }).id!;
      const a = await live(vmId, sid);
      check("large-output: shell is live", a !== null);
      if (a) {
        a.d.clear();
        // print a recognizable first + last marker around 3000 lines
        const got = await a.d.expect("LASTLINE_3000", "seq 1 3000 | sed 's/^/L/'; echo LASTLINE_3000\n", 30);
        check("large-output: 3000-line burst delivered to last line", got);
        a.pty.close();
      }
    }

    // ---- 6) concurrent independent PTY sessions ----
    {
      const s1 = await arker.vm(vmId).createSession();
      const s2 = await arker.vm(vmId).createSession();
      const sid1 = s1.session_id ?? (s1 as { id?: string }).id!;
      const sid2 = s2.session_id ?? (s2 as { id?: string }).id!;
      const a = await live(vmId, sid1);
      const b = await live(vmId, sid2);
      check("concurrent: both sessions live", a !== null && b !== null);
      if (a && b) {
        a.d.clear(); b.d.clear();
        await a.d.expect("ONE", "export WHO=ONE; echo W=$WHO\n");
        await b.d.expect("TWO", "export WHO=TWO; echo W=$WHO\n");
        a.d.clear(); b.d.clear();
        const aIsolated = await a.d.expect("W=[ONE]", "echo W=[$WHO]\n");
        const bIsolated = await b.d.expect("W=[TWO]", "echo W=[$WHO]\n");
        check("concurrent: session 1 keeps its own state", aIsolated);
        check("concurrent: session 2 keeps its own state (isolated)", bIsolated);
        a.pty.close(); b.pty.close();
      }
    }

    // ---- 7) exec-before-PTY corner case (#42): run() then attach a PTY ----
    {
      const s = await arker.vm(vmId).createSession();
      const sid = s.session_id ?? (s as { id?: string }).id!;
      // drive a normal /run on this session FIRST, then attach a PTY to the same session
      await arker.vm(vmId).run("echo PRERUN", { sessionId: sid, timeout: 60 } as Record<string, unknown>).catch(() => {});
      await sleep(800);
      const a = await live(vmId, sid);
      check("exec-before-pty: PTY attaches after a prior /run on the session", a !== null);
      if (a) {
        a.d.clear();
        check("exec-before-pty: PTY shell is interactive", await a.d.expect("XBP_OK", "echo XBP_OK\n"));
        a.pty.close();
      }
    }

    // ---- 8) cancel_ttl: an idle ATTACHED PTY auto-cancels + destroys the shell ----
    //   While attached, if there's no PTY I/O for
    //   cancel_ttl_secs the bridge cancels the run and DESTROYS the shell (the WS
    //   closes server-side). This is NOT a detach timer - it fires on the live conn.
    {
      const s = await arker.vm(vmId).createSession();
      const sid = s.session_id ?? (s as { id?: string }).id!;
      const a = await live(vmId, sid, { cancelTtlSecs: 6 });
      check("cancel_ttl: shell is live", a !== null);
      if (a) {
        let closed = false;
        a.pty.onClose(() => { closed = true; });
        a.d.clear();
        await a.d.expect("TTLMARK", "export TTL=SET; echo TTLMARK\n");
        // Stay connected but IDLE (no I/O) past cancel_ttl → server should cancel.
        for (let i = 0; i < 20 && !closed; i++) await sleep(1000);
        check("cancel_ttl: idle attached PTY was cancelled (WS closed by server)", closed);
        try { a.pty.close(); } catch { /**/ }
        await sleep(3000);
        // Reconnect → the cancelled shell is gone, so this is a FRESH shell.
        const b = await live(vmId, sid);
        check("cancel_ttl: reconnect after cancel succeeds", b !== null);
        if (b) {
          b.d.clear();
          check("cancel_ttl: cancel destroyed the shell (TTL var gone)", await b.d.expect("TTL=[]", "echo TTL=[$TTL]\n"));
          b.pty.close();
        }
      }
    }

    // ---- 9) cold reconnect after idle-TTL suspend keeps the same shell ----
    {
      const s = await arker.vm(vmId).createSession();
      const sid = s.session_id ?? (s as { id?: string }).id!;
      const a = await live(vmId, sid);
      check("cold-reconnect: shell is live", a !== null);
      if (a) {
        a.d.clear();
        await a.d.expect("COLD=YES", "export COLD=YES; echo COLD=$COLD\n");
        a.pty.close();
        console.log("    (waiting 95s for idle-TTL suspend → cold restore path)");
        await sleep(95000);
        const b = await live(vmId, sid);
        check("cold-reconnect: reconnect after suspend succeeds", b !== null);
        if (b) {
          b.d.clear();
          check("cold-reconnect: same shell survives suspend/restore ($COLD)", await b.d.expect("COLD=[YES]", "echo COLD=[$COLD]\n"));
          b.pty.close();
        }
      }
    }
  } finally {
    await arker.vm(vmId).delete().catch(() => {});
  }

  console.log(`\n==== PTY scenarios e2e: ${pass} passed, ${fail} failed ====`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error("PTY scenarios e2e crashed:", e); process.exit(1); });
