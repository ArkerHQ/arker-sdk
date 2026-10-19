#!/usr/bin/env bash
#
# Firmware app-factory — quick start (a coding agent edits firmware, runs it on QEMU)
#
# Forks a caller-selected source, then installs an embedded toolchain and QEMU,
# seeds a Cortex-M3 firmware, has the agent add a feature, then forks a checkpoint
# and has it add another — building on the first.
#
# Prereqs: the Arker CLI (`bun add --global @arker-ai/cli`) and `jq`.
#   ARKER_API_KEY=...  CURSOR_API_KEY=...  ARKER_SOURCE_VM=...  ./quick-start-firmware.sh

set -uo pipefail
: "${ARKER_API_KEY:?set ARKER_API_KEY (https://arker.ai/console)}"
: "${CURSOR_API_KEY:?set CURSOR_API_KEY (https://cursor.com/dashboard)}"
: "${ARKER_SOURCE_VM:?set ARKER_SOURCE_VM to a source that contains Cursor}"

GCC="arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostartfiles -nostdlib -ffreestanding -O2 -T fw.ld fw.c -o fw.elf"
QEMU="qemu-system-arm -M lm3s6965evb -cpu cortex-m3 -nographic -semihosting -kernel fw.elf"
CHANGE2="${1:-after the ticks, print a countdown: 3, then 2, then 1, then DONE}"

# baseline firmware: w0() prints over ARM semihosting (noinline so -O2 can't
# clobber r0); main() just boots. the agent extends this.
FW_C='#include <stdint.h>
__attribute__((noinline)) static void w0(const char *s){
  register int op asm("r0")=0x04; register const char *p asm("r1")=s;
  asm volatile("bkpt 0xAB"::"r"(op),"r"(p):"memory"); }
static void halt(void){
  register int op asm("r0")=0x18; register int c asm("r1")=0x20026;
  asm volatile("bkpt 0xAB"::"r"(op),"r"(c):"memory"); }
int main(void){ w0("firmware: boot OK\n"); halt(); return 0; }
void Reset_Handler(void){ main(); for(;;){} }
__attribute__((section(".vectors"),used))
static void (*const v[])(void) = { (void(*)(void))0x20010000, Reset_Handler };'
FW_LD='ENTRY(Reset_Handler)
MEMORY {
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}
SECTIONS {
  .text : { KEEP(*(.vectors)) *(.text*) *(.rodata*) } > FLASH
  .data : { *(.data*) } > RAM AT > FLASH
  .bss  : { *(.bss*)  } > RAM
}'

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
# run a command in the background and wait for it to finish: agent runs take minutes.
run() {
  local rid
  rid=$(arker run --time-to-background 0 "$1" "{ set +x; } 2>/dev/null; $2" 2>/dev/null | jq -r '.run_id // empty' 2>/dev/null || true)
  [ -n "${rid:-}" ] && until [[ "$(arker runs get --json "$1" "$rid" 2>/dev/null | jq -r .state 2>/dev/null)" =~ ^(completed|failed|cancelled)$ ]]; do sleep 3; done
  return 0
}
show() { arker run "$1" "{ set +x; } 2>/dev/null; cat $2" 2>&1 | grep -vaE '^\+\+? ' || true; }

VM=$(arker fork "$ARKER_SOURCE_VM" | jq -r .vm_id); echo "# forked $VM"

# The selected source contains Cursor. Install only the embedded ARM toolchain.
echo "# install ARM toolchain, seed firmware"
run "$VM" "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends gcc-arm-none-eabi qemu-system-arm >/dev/null"
run "$VM" "mkdir -p /work && echo $(b64 "$FW_C") | base64 -d > /work/fw.c && echo $(b64 "$FW_LD") | base64 -d > /work/fw.ld && printf %s '$CURSOR_API_KEY' > /work/.key"

echo "# agent: add 'tick' x3"
run "$VM" "cd /work && CURSOR_API_KEY=\$(cat .key) cursor-agent -f -p 'Edit /work/fw.c so that after the boot line, main() prints tick three times using the existing w0() helper. Build with $GCC and run with $QEMU; iterate until it runs cleanly.' >/dev/null 2>&1"
run "$VM" "cd /work && { $GCC && $QEMU; } > out.txt 2>&1"
show "$VM" /work/out.txt

# fork a checkpoint carrying the toolchain + firmware so far
CP=$(arker fork --source-vm-id "$VM" | jq -r .vm_id); echo "# forked checkpoint $CP"

echo "# agent (on the fork): $CHANGE2"
run "$CP" "cd /work && CURSOR_API_KEY=\$(cat .key) cursor-agent -f -p 'Edit /work/fw.c to also $CHANGE2, using the existing w0() helper. Keep all prior output. Build with $GCC and run with $QEMU; iterate until it runs cleanly.' >/dev/null 2>&1"
run "$CP" "cd /work && { $GCC && $QEMU; } > out.txt 2>&1"
show "$CP" /work/out.txt

arker rm "$VM" >/dev/null 2>&1 || true
arker rm "$CP" >/dev/null 2>&1 || true
