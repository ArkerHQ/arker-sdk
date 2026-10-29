import os
import re
import sys
import time

paths = set(sys.argv[2:])
deadline = time.monotonic() + float(sys.argv[1])


def decode(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


while True:
    mounted = set()
    with open("/proc/self/mountinfo") as source:
        for line in source:
            left, right = line.split(" - ", 1)
            if right.split()[0] in ("fuse.s3fs", "fuse.mountpoint-s3", "fuse.mount-s3"):
                mounted.add(decode(left.split()[4]))
    if paths <= mounted:
        for path in paths:
            with os.scandir(path) as entries:
                next(entries, None)
        sys.exit(0)
    if time.monotonic() >= deadline:
        sys.exit("Volume mount readiness timed out")
    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
