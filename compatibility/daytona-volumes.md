# Daytona volumes

Python sync and async clients route `volume.create`, `volume.get` and
`volume.list` through the configured providers. Arker-backed values use the
native Daytona `Volume` model and retain their Arker client privately. Names
must resolve to one provider: when multiple configured providers have the same
name, lookup and attachment refuse the ambiguity. Authentication and tenant
errors do not become misses or trigger creation elsewhere.

`ready` describes the committed logical filesystem namespace. Arker creation
writes its marker and catalog record synchronously; there is no asynchronous
namespace provisioning or metadata update operation. `updated_at` therefore
matches `created_at`, `error_reason` is null, and unknown `last_used_at` stays
null. These fields do not describe mount convergence or the last file write.

Whole-filesystem attachments accept native `VolumeMount` parameters. Creation
preflights identities and canonical mount paths, attaches each filesystem, and
waits for its actual guest FUSE mount to be accessible to the existing non-root
`daytona` account. Failure deletes the newly owned sandbox. Mounted storage uses
the backend's coherence model, not a promise of POSIX locking or instantaneous
cross-VM cache invalidation.

## Explicit gaps

Arker-backed `volume.delete` is refused before issuing a destructive request.
Native Daytona refuses deletion while a sandbox has the volume mounted;
Arker's generic filesystem deletion removes attachments and contents. Safe
native-compatible deletion needs a backend atomic unmounted condition.
Applications using the explicit Arker filesystem API retain its own deletion
contract.

Nonempty `subpath` is refused before sandbox creation. The backend must persist
and restore the subtree and enforce its storage credential prefix; guest
symlinks or bind mounts are not substitutes. Potentially truncated 500-entry
filesystem listings are refused. The server needs complete pagination for larger
inventories. TypeScript volume service routing is not supplied by this patch.

## Differential checks

`python/tests/e2e/test_daytona_volumes_import_only.py` runs the same workload
across native, routed-native and Arker clients, in sync and async styles. It
covers same-name get/create races, duplicate-create errors, mounted deletion,
and cross-VM binary writes, rename and unlink. Mounted deletion remains an
expected uncovered contract on Arker, not a passing parity claim. Arker fixture
teardown uses explicit storage deletion only after removing the owned VMs and
supplies no evidence for Daytona volume deletion.

The Arker source must provide Python3, `setpriv` and a writable home for its
non-root `daytona` account. The current Linux storage driver uses UID/GID1000;
`python/tests/e2e/daytona_baseline.py` prepares that identity only in its own
pinned disposable image, with an explicit platform. It never changes a
customer-selected source.
