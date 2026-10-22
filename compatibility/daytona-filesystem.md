# Daytona filesystem contract

The Python sync/async and TypeScript adapters provide directory creation/listing,
file metadata, buffered upload/download, rename, deletion and permission changes.
Commands and filesystem access run as the source's existing non-root `daytona`
account. Relative paths use its home. The adapters do not create accounts or
silently fall back to root.

Select a suitable parent with `ARKER_SOURCE_VM_ID` or `ARKER_SOURCE`, and configure
`ARKER_PROVIDER` plus `ARKER_REGION` (or an explicit API base URL). Credentials
alone do not select an image or placement. The source needs Python 3, `setpriv`
and an accessible home for `daytona`. A prepared parent is an environment
prerequisite, not native default-image parity.

Transfers reserve private staging paths, open guest files after dropping
privileges, reject special files and preserve the primary error during cleanup.
Batch writes are sequential and may partially succeed. Buffered transfers are
not streaming APIs and do not promise bounded client memory or atomic writes.

## Verification

The differential filesystem program executes unchanged across reference,
shim-native and shim-Arker routes, checking CRUD, metadata, permission denial and
command/filesystem identity agreement. Local tests additionally cover binary
content, FIFOs, staging collisions and cleanup. Use fresh evidence paths and a
run ID after freezing runtime and scenario files:

```sh
COMPAT_COVERAGE_RUN=daytona-filesystem COMPAT_COVERAGE_EVIDENCE=/tmp/daytona-python.jsonl python/.venv/bin/pytest -q python/tests/e2e/test_daytona_filesystem_import_only.py
cd typescript
COMPAT_COVERAGE_RUN=daytona-filesystem COMPAT_COVERAGE_EVIDENCE=/tmp/daytona-typescript.jsonl bun tests/compat-daytona-filesystem-live.ts
```

## Remaining gaps

Custom users, Python-free images, image `WORKDIR`, native shell selection,
streaming/cancellation, custom request timeouts, search/find/replace/watch,
special permission bits and complete symlink/error/timestamp equivalence are
not established. Home-directory accessors have unit coverage; that does not
supply live evidence for those methods.

References: [filesystem guide](https://www.daytona.io/docs/file-system-operations/),
[Python filesystem API](https://www.daytona.io/docs/en/python-sdk/sync/file-system/),
and the installed SDK versions pinned by this repository.
