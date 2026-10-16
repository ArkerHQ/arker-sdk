# Recorded coverage baseline

Measured against the pinned vendor SDKs on 2026-10-16. Counts below are public
entries within the bounded inventory described in `README.md`, not claims that
each entry's option combinations or error paths are equivalent.

| Provider | Python discovered | Python scenario-tested | TypeScript discovered | TypeScript scenario-tested |
| --- | ---: | ---: | ---: | ---: |
| Blaxel | 278 | 30 | 154 | 15 |
| Daytona | 258 | 0 | 234 | 0 |
| E2B | 201 | 0 | 118 | 0 |
| Modal | 207 | 0 | 422 | 0 |
| Total | 944 | 30 | 928 | 15 |

Zero means no recorded differential evidence in the new harness. It does not
erase previous testing or assert that every remaining entry is unimplemented.
Both `--fail-on-unverified` gates return exit code 1.

The passing Blaxel program runs unchanged against the original SDK, the shim on
Blaxel, and the shim on Arker, in Python sync, Python async, and TypeScript. It
checks foreground output, cwd/environment isolation, file overwrite/listing,
reconnect, named background commands, stdin/EOF, status/logs/list, timeout of
observation without killing the command, and process kill. Local protocol tests
also cover execution timeout, duplicate-name rejection, nonzero exit, callbacks,
and cancellation. These local tests do not grant differential coverage.

## Open release requirements

- Remove the guest process agent's Python 3 dependency or provide an
  image-independent runtime. A real Arker `ubuntu:24.04` image probe confirmed
  that Python 3 is absent. The current background process implementation was
  validated on `ubuntu-coding`, not arbitrary images.
- Instrument and extend the existing E2B, Modal, and Daytona comparisons.
- Cover returned handles and option/error/cancellation combinations, not merely
  a method's presence or a single successful call.
- Close the remaining Arker-side APIs exposed as missing/unverified by inventory,
  including provider-specific services, lifecycle options, and streaming APIs.
- Release the ComputeSDK provider fixes before the migration SDK release.

Full API equivalence has not been established. The three PRs remain drafts.
