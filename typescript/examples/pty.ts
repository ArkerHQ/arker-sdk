/**
 * Interactive PTY example - open a real terminal in a VM and run `claude`.
 *
 *   ARKER_API_KEY=...  ARKER_BASE_URL=https://<host>/api \
 *     bun examples/pty.ts [vm_id]
 *
 * Drops your local terminal into a live shell inside the VM. If `claude` isn't
 * already installed in the image, install it from the shell first, e.g.
 *   bun add --global @anthropic-ai/claude-code   # then run: claude
 *
 * Exit by leaving the shell (`exit` / Ctrl-D).
 */
import { Arker } from "../src/index.js";

async function main() {
  const ar = new Arker(); // reads ARKER_API_KEY + ARKER_REGION/ARKER_BASE_URL

  const arg = process.argv[2];
  const source = process.env.ARKER_SOURCE_VM;
  if (!arg && !source) throw new Error("pass a VM id or set ARKER_SOURCE_VM");
  const vm = arg ? await ar.vm(arg).refresh() : await ar.fork({ source_vm_name: source! });
  if (!arg) console.error(`forked ${vm.id}`);

  const stdin = process.stdin;
  const stdout = process.stdout;
  let closed = false;

  const pty = await vm.createPty({
    cols: stdout.columns ?? 80,
    rows: stdout.rows ?? 24,
    onData: (bytes) => stdout.write(bytes),
    onExit: () => {
      closed = true;
      if (stdin.isTTY) try { stdin.setRawMode(false); } catch {}
      process.exit(0);
    },
  });

  if (stdin.isTTY) stdin.setRawMode(true);
  stdin.resume();
  stdin.on("data", (c: Buffer) => { if (!closed) void pty.sendInput(new Uint8Array(c)); });
  stdout.on("resize", () => {
    if (!closed) void pty.resize({ cols: stdout.columns ?? 80, rows: stdout.rows ?? 24 });
  });

  console.error("[connected] you're in the VM. Try: claude  (install first if needed)");
  await new Promise<void>(() => {});
}

main().catch((e) => { console.error(e); process.exit(1); });
