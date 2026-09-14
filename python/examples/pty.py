"""Interactive PTY example — open a real terminal in a VM and run `claude`.

    pip install 'arker[pty]'
    ARKER_API_KEY=...  ARKER_BASE_URL=https://<host>/api \
        python examples/pty.py [vm_id]

Drops your local terminal into a live shell inside the VM. If `claude` isn't
already installed in the image, install it from the shell first, e.g.
    bun add --global @anthropic-ai/claude-code   # then run: claude

Exit by leaving the shell (`exit` / Ctrl-D).
"""

import os
import sys
import termios
import tty

from arker import Arker


def main() -> None:
    ar = Arker()  # reads ARKER_API_KEY + ARKER_REGION/ARKER_BASE_URL

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    vm = ar.vm(arg).refresh() if arg else ar.fork(source_vm_name=os.environ["ARKER_SOURCE_VM"])
    if not arg:
        print(f"forked {vm.id}", file=sys.stderr)

    cols, rows = os.get_terminal_size() if sys.stdout.isatty() else (80, 24)

    def on_data(b: bytes) -> None:
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()

    pty = vm.connect_pty(cols=cols, rows=rows, on_data=on_data)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if sys.stdin.isatty() else None
    if old is not None:
        tty.setraw(fd)  # forward every keystroke (incl. Ctrl-C) verbatim
    print(
        "[connected] you're in the VM. Try: claude  (install first if needed)",
        file=sys.stderr,
    )
    try:
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            pty.send_input(chunk)
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        pty.kill()


if __name__ == "__main__":
    main()
