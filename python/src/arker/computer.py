"""Arker Python SDK.

A small wrapper around the VM API. Configure a region for the standard Arker
endpoints, or pass base_url directly for internal/dev targets.
"""

from __future__ import annotations

import atexit
import base64
import dataclasses
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import tarfile
import tempfile
import threading
import time
import types
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Literal, TypeVar, get_args, get_origin, get_type_hints

import httpx

from .generated.api_models import (
    BackgroundRunResponse,
    CancelRunResponse,
    CompletedRunResponse,
    CreateSessionRequest,
    DeleteFilesystemResponse,
    DeleteSessionResponse,
    DeleteSyncResponse,
    DeleteVmResponse,
    ErrorResponse,
    Filesystem,
    FilesystemCreateRequest,
    ForkRequest1,
    ForkRequest2,
    ForkRequest3,
    ForkRequest4,
    ListFilesystemsResponse,
    ListFilesystemsParameters,
    ListOrgRunsResponse,
    ListOrgRunsParameters,
    ListRegionsResponse,
    ListRunsResponse,
    ListRunsParameters,
    ListSessionsResponse,
    ListSessionsParameters,
    ListSyncsResponse,
    ListSyncsParameters,
    ListVmsResponse,
    ListVmsParameters,
    OrgRunListRow,
    PatchSessionRequest,
    PatchSessionResponse,
    PatchVmRequest,
    PolicyDoc,
    RegistryAuth,
    PtyTicketResponse,
    RegionPlacement,
    Run,
    RunRequest,
    RunResponse,
    RunSummary,
    Session,
    Sync,
    SyncChunkWrite,
    SyncCreateRequest,
    SyncManifestOperationRequest,
    SyncManifestResponse,
    SyncPresignedWriteCommit,
    SyncPresignedWriteRequest,
    SyncPresignedWriteRequestResult,
    SyncReadInlineResponse,
    SyncReadOperationRequest,
    SyncReadPresignedResponse,
    SyncReadResponse,
    SyncWriteEntry,
    SyncWriteOperationRequest,
    SyncWriteResponse,
    SyncWriteResult,
    Vm,
    VmNetwork,
    ResourcesInput,
    VmResources,
)

Model = TypeVar("Model")


class _UnsetType:
    pass


class _ExplicitNullType:
    pass


_UNSET = _UnsetType()
_EXPLICIT_NULL = _ExplicitNullType()

CHUNK_SIZE = 4 * 1024 * 1024
# Max decoded bytes carried in ONE /sync request's `writes[]` (as CHUNK_SIZE
# chunks sharing an upload_id). arkerd enforces `MAX_SYNC_INLINE_REQUEST_BYTES
# = 20 MiB` (aws/arkerd-worker-linux/src/api/routes/sync.rs) on the decoded
# total per request; this stays comfortably under that so one JSON/base64
# overhead miscalculation never tips a request over the server's hard cap.
# `_sync_write_inline` batches into multiple sequential requests sharing one
# `upload_id` — reassembled server-side by the same `SyncChunkSessionState`
# ledger that lets a single request already carry multiple chunks — so an
# inline write of any size degrades to more round trips instead of failing.
INLINE_WRITE_LIMIT = 16 * 1024 * 1024

# Largest single request body the service accepts. A larger body is rejected
# with 413 `payload_too_large`; the limit is exact and fails loudly, never
# truncating.
STREAM_MAX_BYTES = 64 * 1024 * 1024

# A single file at or above this size is uploaded as an archive rather than as
# a plain byte body, so its mode bits travel with it.
ARCHIVE_MIN_BYTES = 1024 * 1024

# Ceiling on one archive's total INPUT bytes before a directory sync splits the
# changed-file set into multiple sequential tarballs instead of one.
#
# arkerd's raw `/sync` transport enforces
# `MAX_SYNC_FILE_BYTES = 2 GiB` per request (aws/arkerd-worker-linux/src/api/routes/sync.rs)
# and rejects anything larger with 413 `payload_too_large` -- exact, no
# truncation. A directory sync used to tar EVERY changed file into ONE archive
# regardless of total size, so a cold sync of a tree with >2 GiB of changed
# content (or a warm sync that happens to touch that much) failed outright
# rather than merely being slow. Bounded well under the hard cap: tar's
# per-file overhead (a 512-byte header, padded to a 512-byte boundary) is
# trivial even across thousands of files, but an incompressible payload
# uploads at close to its raw size, and the margin protects against exactly
# that worst case.
ARCHIVE_BATCH_MAX_BYTES = 1_500_000_000

# Archive batches uploaded concurrently once a directory sync needs more than
# one (see ARCHIVE_BATCH_MAX_BYTES). Each batch extracts a disjoint set of
# files under the same `remote_root`, so concurrent `tar -x` calls never touch
# the same path.
ARCHIVE_BATCH_CONCURRENCY = 4

# Below this a compressibility sample is not worth taking; above it `sync`
# samples the payload before deciding whether to compress it.
COMPRESSION_SAMPLE_MIN_BYTES = 256 * 1024
# Compress only when a sample sees at least this much reduction. Payloads that
# barely compress cost time on both ends for nothing.
#
# KEEP IN SYNC: arker-app's benchmark harness
# (benchmarks/benchrunner/providers/arker.py, `_COMPRESSION_WORTH_IT_RATIO`)
# hardcodes this same value to model real caller behavior. There is no
# automated check tying the two together — update both in the same change.
COMPRESSION_WORTH_IT_RATIO = 0.6
# Fastest gzip setting. Higher settings buy ~1% more reduction for ~3x the
# time, which loses on any link fast enough to matter.
COMPRESSION_LEVEL = 1

# Files hashed concurrently. hashlib releases the GIL, so this is real CPU
# parallelism; bounded to keep open file descriptors sane.
HASH_CONCURRENCY = 8
# Files downloaded concurrently when pulling a directory out of a VM.
DOWNLOAD_CONCURRENCY = 8

# Org id for callers that explicitly select an Arker-owned public source.
ARKER_ORG_ID = "ArkerHQ"

DEFAULT_RETRY_ATTEMPTS = 4
DEFAULT_RETRY_BASE_DELAY_S = 0.2
DEFAULT_RETRY_MAX_DELAY_S = 2.0
DEFAULT_RETRY_JITTER_S = 0.05
# ── Synchronous run() auto-poll ────────────────────────────────────
# When a run outlives its server-side sync window (``time_to_background``),
# the API hands back a background ack carrying a run_id. For a synchronous
# caller (one that did not ask to background) run() then polls get_run()
# under the hood until the run reaches a terminal state and returns the
# completed run — so the caller transparently gets the final result.
RUN_POLL_INITIAL_S = 0.5
RUN_POLL_MAX_S = 3.0
RUN_POLL_BACKOFF = 1.5
# Slack beyond the run's kill bound before we stop polling and raise a timeout.
RUN_POLL_MARGIN_S = 30.0
# An unbounded wait is not an infinite one. What still ends it is the SERVICE
# becoming unreachable: this many CONSECUTIVE failed status checks. Any
# answered check resets the counter (a run reported as still ``running`` is a
# successful check), so a long-running command and a transient network blip
# both survive; only a service that has stopped responding raises.
RUN_POLL_MAX_CONSECUTIVE_FAILURES = 10


def run_poll_budget_s(timeout: int | None) -> float | None:
    """How long run()'s poll may wait for a backgrounded run, in seconds —
    ``None`` for no limit.

    An unset or ``0`` timeout is unbounded server-side, so the poll is
    unbounded too: giving up at a client-side deadline the caller never asked
    for would abandon a run that is still going.
    """
    if timeout is None or timeout <= 0:
        return None
    return timeout + RUN_POLL_MARGIN_S
# Terminal run states — RunState ("pending" | "running" | "completed" |
# "failed" | "cancelled") minus the two NON-terminal states, "pending" and
# "running". A run is "pending" while it waits behind an earlier run on the
# same session; a poller must keep polling through it. Anything not in this
# set is treated as non-terminal, so an unknown future state degrades to
# "keep polling" rather than a false completion.
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})
PRESIGNED_PUT_TIMEOUT_S = 600
# Must exceed the server's 120s sync window, or the request is given up on just
# as the background ack arrives and run() never gets to poll.
REQUEST_TIMEOUT_S = 300
RETRYABLE_HTTP = {429, 502, 503, 504}
RETRYABLE_CODES = {
    "unavailable",
    "bad_gateway",
    "stale_route",
    "capacity_unavailable",
}
TRANSIENT_HINTS = ("503", "Service Unavailable", "throttle", "SlowDown", "ThrottlingException")
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_REGION_ENV = "ARKER_REGION"
DEFAULT_PROVIDER_ENV = "ARKER_PROVIDER"
ComputeProvider = str
DEFAULT_CONTROL_BASE_URL = "https://arker.ai/api"

@dataclasses.dataclass(frozen=True)
class RetryOptions:
    attempts: int = DEFAULT_RETRY_ATTEMPTS
    base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S
    # Caps backoff delays; when set explicitly, also caps a server retry_after
    # hint. None = the default backoff cap, which does not bound the hint.
    max_delay_s: float | None = None
    jitter_s: float = DEFAULT_RETRY_JITTER_S


# ── Resources ───────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class VmList:
    vms: list[VM]
    next_cursor: str | None = None

    @property
    def items(self) -> list[VM]:
        return self.vms

    @property
    def total(self) -> int:
        return len(self.vms)

    def __iter__(self):
        return iter(self.vms)

    def __len__(self) -> int:
        return len(self.vms)


def _as_text(data: bytes) -> str:
    """Decode command output for the caller-facing ``stdout``/``stderr``.

    Never raises: bytes that are not valid UTF-8 become U+FFFD. Defined once so
    the conversion cannot drift between result types.
    """
    return data.decode("utf-8", "replace")


@dataclasses.dataclass(frozen=True)
class RunRecord(Run):
    """A fetched run. Output is available as text and as exact bytes.

    ``stdout``/``stderr`` are decoded text, ready to print or match on.
    ``stdout_bytes``/``stderr_bytes`` are exactly what the command wrote — use
    those when the output is not text (an image, an archive, anything binary),
    because decoding to text replaces undecodable bytes and cannot be undone.

    Subclasses the generated model rather than restating its fields, so schema
    additions flow through. ``Run`` stays the exact wire shape; on this type the
    text fields hold the *decoded* output rather than the encoded wire value.
    """

    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


@dataclasses.dataclass(frozen=True)
class CompletedRunResult:
    stdout: str
    stderr: str
    stdout_bytes: bytes
    stderr_bytes: bytes
    exit_code: int
    run_id: str | None = None  # present for executed runs; None for operation acks
    # The session this run used. A run always occupies exactly one, and it could
    # not otherwise be learned: `session_idx` is find-or-create and `session_id`
    # is assigned server-side. None for operation acks, which run no command.
    session_id: str | None = None
    state: str = "completed"   # "completed" | "failed"; mirrors the run-status (Run) shape
    # System failure explanation when state is "failed"; distinct from
    # stderr (the program's own error output). None otherwise.
    fail_reason: str | None = None
    memory_requested_mib: int | None = None
    memory_achieved_mib: int | None = None
    memory_partial: bool = False
    type: str = "completed"


@dataclasses.dataclass(frozen=True)
class BackgroundRunResult:
    run_id: str
    # The session this run is executing in. Matters most on THIS shape: a
    # backgrounded process outlives the call, and this is how you find it again
    # to inspect or stop it without guessing the index it landed on.
    session_id: str | None = None
    state: str = "running"
    type: str = "background"


# Result of VM.run(). A synchronous call (``time_to_background`` not zero) always
# returns a CompletedRunResult — if the run outlives its sync window run()
# polls it to completion under the hood. Only explicit ``time_to_background=0``
# yields a BackgroundRunResult (the running ack, returned immediately for the
# caller to poll via VM.get_run()).
RunResult = CompletedRunResult | BackgroundRunResult


@dataclasses.dataclass(eq=False)
class ArkerError(Exception):
    code: str
    message: str
    status: int

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")


# ── Client ──────────────────────────────────────────────────────────


def discover_regions(
    *,
    control_base_url: str | None = None,
    retry: RetryOptions | dict[str, Any] | bool | None = None,
) -> ListRegionsResponse:
    """Read the public placement catalog without configuring compute or auth."""
    base_url = _normalize_base_url(
        control_base_url
        or _env("ARKER_CONTROL_BASE_URL")
        or DEFAULT_CONTROL_BASE_URL
    )
    payload = _request_json(
        "GET",
        "/v1/regions",
        base_url=base_url,
        retry=_normalize_retry(retry),
    )
    return _decode_model(ListRegionsResponse, payload)


class Arker:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        control_base_url: str | None = None,
        region: str | None = None,
        provider: ComputeProvider | None = None,
        retry: RetryOptions | dict[str, Any] | bool | None = None,
    ) -> None:
        resolved_api_key = api_key or _env("ARKER_API_KEY") or _env("AUTH_KEY")
        explicit_base_url = base_url or _env("ARKER_BASE_URL")
        raw_region = region or (None if explicit_base_url else _env(DEFAULT_REGION_ENV))
        raw_provider = provider or (None if explicit_base_url else _env(DEFAULT_PROVIDER_ENV))
        if not explicit_base_url and bool(raw_provider) != bool(raw_region):
            raise ValueError("provider and region are required together unless base_url is supplied")
        provider_value = _normalize_placement_label("provider", raw_provider) if raw_provider else None
        resolved_region = _normalize_placement_label("region", raw_region) if raw_region else None

        resolved_base_url = explicit_base_url or (
            _compute_base_url(provider_value, resolved_region)
            if provider_value and resolved_region
            else None
        )
        resolved_control_base_url = (
            control_base_url
            or _env("ARKER_CONTROL_BASE_URL")
            or DEFAULT_CONTROL_BASE_URL
        )

        if not resolved_api_key:
            raise ValueError("api_key is required; pass api_key or set ARKER_API_KEY")
        if not resolved_base_url:
            raise ValueError(
                "provider and region or base_url are required; pass provider and region, "
                "base_url, ARKER_PROVIDER and ARKER_REGION, or ARKER_BASE_URL"
            )

        self._api_key = resolved_api_key
        self._base_url = _normalize_base_url(resolved_base_url)
        self._control_base_url = _normalize_base_url(resolved_control_base_url)
        self._region = resolved_region
        self._provider = provider_value
        self._retry = _normalize_retry(retry)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def control_base_url(self) -> str:
        return self._control_base_url

    @property
    def region(self) -> str | None:
        return self._region

    @property
    def provider(self) -> ComputeProvider | None:
        return self._provider

    def vm(
        self,
        vm_id: str,
        *,
        provider: ComputeProvider | None = None,
        region: str | None = None,
    ) -> "VM":
        return VM(self, vm_id, self._base_url_for(vm_id, provider=provider, region=region))

    def fork(
        self,
        source: "VM | str | None" = None,
        *,
        source_vm_id: str | None = None,
        source_vm_name: str | None = None,
        source_org_id: str | None = None,
        image: str | None = None,
        dockerfile: str | None = None,
        registry_auth: "RegistryAuth | dict[str, str] | None" = None,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
        ssh_public_keys: list[str] | None = None,
        disk: bool | None = None,
        vcpu_count: int | None = None,
        memory_mib: int | None = None,
        disk_mib: int | None = None,
        vgpu: float | None = None,
        durable: bool | None = None,
        platforms: list[str] | None = None,
        layers: list[str] | None = None,
        policies: PolicyDoc | dict[str, Any] | None = None,
        queueing_timeout: int | None = None,
    ) -> "VM":
        """Create a new VM by forking from a source.

        The source can be passed positionally or by keyword:

        - ``fork(source_name)`` — fork an API-returned source by name.
        - ``fork("base")`` — fork a VM by name in your own org.
        - ``fork(vm)`` — fork an existing ``VM`` (uses its id).
        - ``fork(source_vm_id="vm_abc...")`` — fork by global id.
        - ``fork(source_vm_name="base", source_org_id="org_...")`` — fork a
          named VM in a specific org (it must be ``public``).
        - ``fork(image="ubuntu:24.04")`` — create a VM from an OCI image
          instead of an existing VM. A bare reference, never a URI: an
          unqualified name resolves against Docker Hub, so ``ubuntu:24.04`` is
          ``docker.io/library/ubuntu:24.04`` and ``ghcr.io/org/img:v1`` is not.
          Exclusive with the VM selectors. The image is pulled and converted on
          the host, so the first fork of an image is slow (tens of seconds) and
          later ones reuse the cached layers. Inputs that only mean something
          relative to a source VM (``layers``, ``public`` and the GPU fields)
          are rejected by the service rather than silently ignored.

          ``platforms`` MATTERS here, and is served. The image is pulled for
          the architecture of whatever host the request lands on, so an image
          published only for amd64 (``pytorch/pytorch``, for one) fails on an
          arm64 host: it converts and boots, then the VM cannot execute
          anything in its own filesystem. Pin an x86 platform for such an
          image::

              vm = arker.fork(image="pytorch/pytorch:latest",
                              platforms=["icelake"])

          The image's ``ENV``, ``USER`` and ``WORKDIR`` ARE applied, so a tool
          on the image's own ``PATH`` runs without a prefix and the first run
          starts in the image's working directory::

              vm.run('python -c "import torch"')   # /opt/conda/bin is on PATH

          ``CMD``/``ENTRYPOINT`` are recorded but deliberately NOT started.
          A VM whose entrypoint was running would be busy from birth, which
          blocks idle suspend; start the workload yourself when you want it::

              vm.run("nginx -g 'daemon off;' &")

        - ``fork(dockerfile="FROM ubuntu:24.04\nRUN ...")`` — build from a
          Dockerfile instead of pulling a single image. Each instruction runs
          on the host and the result is forked like any other image.

        ``registry_auth={"username": ..., "password": ...}`` supplies
        credentials for a private registry, and applies to the ``image`` and
        ``dockerfile`` sources only (including a private *base* image in a
        Dockerfile). Credentials authorize one pull; they are not stored, and a
        child fork of the resulting VM needs none. Passing them without an
        image or dockerfile is refused rather than silently ignored.

        ``source_org_id`` is sent only when supplied explicitly. The service
        resolves omitted ownership from its current source catalog and the
        caller's organization. It is irrelevant when forking by id. ``name``
        (optional) is the *new* VM's name in your org. ``description`` is a
        short description owned by the new VM and is never inherited.

        ``policies`` is the child's network policy document. Omit it to inherit
        a copy of the source VM's policy. Pass a document to replace it — even an empty
        ``{"policies": []}``, which clears to allow-all rather than inheriting.
        Pass ``ssh_public_keys`` to authorize keys on the new VM.

        ``vgpu`` sizes a GPU in eighths of one card: ``0.125``, ``0.25``,
        ``0.375``, ``0.5``, ``0.625``, ``0.75``, ``0.875``, or ``1``. Anything
        between two steps is refused. It is a fraction of whichever card serves
        the fork, so the same value is a different slice per platform —
        ``vgpu=0.25`` is 35 SMs and 11517 MiB on an L40S, and rather more on a
        B200. The VM reports back the ``gpu_sms`` and ``gpu_vram_mib`` the
        fraction resolved to.

        ``layers`` selects which layers of the source the child inherits. Omit
        it for the default full fork (``["disk", "memory"]``): the child inherits
        both the filesystem and a copy of the source's live RAM, so it resumes
        as an exact continuation — same processes, same shell state. Pass
        ``["disk"]`` for a disk-only fork: the child inherits only the
        filesystem and cold-boots with fresh RAM. Everything on disk survives
        (installed packages, checked-out source, files the parent wrote);
        nothing in memory does (no inherited processes, no environment, no
        shell state).

        ``queueing_timeout`` (seconds) queues instead of failing fast: retries
        until the window elapses, then surfaces the error. ``None``/``0`` =
        fail fast.
        """
        # Positional source: a VM handle (use its id) or a name string.
        if source is not None:
            if source_vm_id or source_vm_name:
                raise ArkerError("bad_request", "fork: pass the source positionally or by keyword, not both", 400)
            if isinstance(source, VM):
                source_vm_id = source.id
            elif isinstance(source, str):
                source_vm_name = source
            else:
                raise ArkerError("bad_request", "fork source must be a VM or a source-name string", 400)
        # `image` and `dockerfile` are two further sources, each exclusive
        # with the VM selectors and with each other. A body naming more than
        # one decodes as no variant of the contract's `oneOf`.
        if image and dockerfile:
            raise ArkerError("bad_request", "fork: pass an image or a dockerfile, not both", 400)
        if (image or dockerfile) and (source_vm_id or source_vm_name):
            raise ArkerError("bad_request", "fork: pass a source VM or an image/dockerfile, not both", 400)
        if not source_vm_id and not source_vm_name and not image and not dockerfile:
            raise ArkerError("bad_request", "fork requires a source (a VM, a name, source_vm_name, source_vm_id, image, or dockerfile)", 400)
        # Credentials authorize a registry pull. With no pull to perform they
        # would be sent for nothing, so refuse rather than quietly drop them.
        if registry_auth is not None and not (image or dockerfile):
            raise ArkerError("bad_request", "fork: registry_auth applies to an image or dockerfile fork", 400)
        if source_vm_id and source_vm_name:
            raise ArkerError("bad_request", "fork: pass only one of source_vm_id or source_vm_name", 400)
        # The contract folds vcpu/memory/disk/gpu into a single `resources` object.
        # GPU bounds are per-platform (`Vm.gpu_platforms`); a request above a
        # platform's max is a 400 from the server, not a silent clamp.
        resources: ResourcesInput | None = None
        if any(v is not None for v in (vcpu_count, memory_mib, disk_mib, vgpu)):
            resources = ResourcesInput(
                vcpu=vcpu_count,
                memory_mib=memory_mib,
                disk_mib=disk_mib,
                vgpu=vgpu,
            )
        policy_doc = (
            policies
            if isinstance(policies, PolicyDoc)
            else _decode_model(PolicyDoc, policies)
            if policies is not None
            else None
        )
        auth = (
            registry_auth
            if isinstance(registry_auth, RegistryAuth)
            else _decode_model(RegistryAuth, registry_auth)
            if registry_auth is not None
            else None
        )
        request_options = dict(
            source_org_id=source_org_id,
            name=name,
            description=description,
            public=public,
            ssh_public_keys=ssh_public_keys,
            # Do NOT default `disk` to True. A source without a disk rejects a
            # disk-backed fork. Omitting it lets the server inherit the source
            # configuration. Only emit this field when the caller opts in.
            disk=disk,
            durable=durable,
            platforms=platforms,
            layers=layers,
            resources=resources,
            policies=policy_doc,
            registry_auth=auth,
            queueing_timeout=queueing_timeout,
        )
        body = (
            # Truthiness, matching the exclusivity checks above and the TS SDK:
            # `image=""` is caller error, and treating it as "an image was
            # given" would discard a `source_vm_name` the caller did supply.
            ForkRequest3(image=image, **request_options)
            if image
            else ForkRequest4(dockerfile=dockerfile, **request_options)
            if dockerfile
            else ForkRequest1(source_vm_id=source_vm_id, **request_options)
            if source_vm_id is not None
            else ForkRequest2(source_vm_name=source_vm_name, **request_options)
        )
        base_url = source.base_url if isinstance(source, VM) else self._base_url
        payload = self._request(
            "POST", "/v1/fork", body, base_url=base_url, max_queueing_s=queueing_timeout
        )
        info = _vm_info(payload)
        # Child lives on the host the fork was posted to.
        return VM(self, info.vm_id, base_url, info)

    def list_vms(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        region: str | None = None,
        provider: str | None = None,
        org_id: str | None = None,
        public: bool | None = None,
        state: str | None = None,
    ) -> VmList:
        """Admin call — goes through the control plane so it can
        aggregate across providers and regions.
        """
        parameters = ListVmsParameters(
            cursor=cursor,
            limit=limit,
            region=region,
            provider=provider,
            org_id=org_id,
            public=public,
            state=state,
        )
        path = _build_query("/v1/vms", parameters)
        payload = self._request("GET", path, base_url=self._control_base_url)
        vms = []
        for item in payload.get("vms", []):
            info = _vm_info(item)
            vms.append(VM(
                self,
                info.vm_id,
                self._base_url_for(info.vm_id, provider=info.provider, region=info.region),
                info,
            ))
        return VmList(vms=vms, next_cursor=_optional_str(payload.get("next_cursor")))

    def list_runs(
        self,
        *,
        since: int | None = None,
        until: int | None = None,
        vm: str | None = None,
        vm_ids: list[str] | None = None,
        region: str | None = None,
        provider: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        lite: bool | None = None,
        runtime: str | None = None,
        endpoint: str | None = None,
        actions: list[str] | None = None,
        status: list[str] | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        sort: str | None = None,
        dir: str | None = None,
    ) -> ListOrgRunsResponse:
        """List run activity across VMs through the control plane."""
        parameters = ListOrgRunsParameters(
            since=since,
            until=until,
            vm=vm,
            vms=",".join(vm_ids) if vm_ids else None,
            region=region,
            provider=provider,
            search=search,
            limit=limit,
            offset=offset,
            lite=lite,
            runtime=runtime,
            endpoint=endpoint,
            actions=",".join(actions) if actions else None,
            status=",".join(status) if status else None,
            status_min=status_min,
            status_max=status_max,
            sort=sort,
            dir=dir,
        )
        path = _build_query("/v1/runs", parameters)
        payload = self._request("GET", path, base_url=self._control_base_url)
        return _org_runs_response(payload)

    def list_regions(self) -> ListRegionsResponse:
        """List available public provider and region placements."""
        payload = self._request("GET", "/v1/regions", base_url=self._control_base_url)
        return _decode_model(ListRegionsResponse, payload)

    def get_vm(
        self,
        vm_id: str,
        *,
        provider: ComputeProvider | None = None,
        region: str | None = None,
    ) -> VM:
        base_url = self._base_url_for(vm_id, provider=provider, region=region)
        info = _vm_info(self._request("GET", _vm_path(vm_id), base_url=base_url))
        return VM(self, vm_id, base_url, info)

    # ── Filesystems (org-scoped, control-plane) ─────────────────────────
    def list_filesystems(self, *, cursor: str | None = None, limit: int | None = None, name_prefix: str | None = None) -> ListFilesystemsResponse:
        parameters = ListFilesystemsParameters(
            cursor=cursor, limit=limit, name_prefix=name_prefix
        )
        path = _build_query("/v1/filesystems", parameters)
        # Filesystems are region-scoped. Route to the regional endpoint
        # (base_url) rather than the control plane, which does not serve
        # /v1/filesystems.
        payload = self._request("GET", path, base_url=self._base_url)
        return _decode_model(ListFilesystemsResponse, payload)

    def create_filesystem(self, *, name: str) -> Filesystem:
        request = FilesystemCreateRequest(name=name)
        return _filesystem(self._request("POST", "/v1/filesystems", request, base_url=self._base_url))

    def get_filesystem(self, filesystem_id: str) -> Filesystem:
        return _filesystem(self._request("GET", f"/v1/filesystems/{_segment(filesystem_id)}", base_url=self._base_url))

    def delete_filesystem(self, filesystem_id: str) -> DeleteFilesystemResponse:
        payload = self._request("DELETE", f"/v1/filesystems/{_segment(filesystem_id)}", base_url=self._base_url)
        return _decode_model(DeleteFilesystemResponse, payload)

    def _request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
        max_queueing_s: int | None = None,
    ) -> dict[str, Any]:
        return _request_json(
            method,
            path,
            body,
            base_url=base_url or self._base_url,
            retry=self._retry,
            api_key=self._api_key,
            extra_headers=extra_headers,
            max_queueing_s=max_queueing_s,
        )

    def _retry_delay(self, attempt: int) -> float:
        return _retry_delay(self._retry, attempt)

    def _base_url_for(
        self,
        ref: str,
        *,
        provider: object = None,
        region: str | None = None,
    ) -> str:
        placement_provider = _optional_compute_provider(provider)
        if placement_provider and region and region.strip():
            return _compute_base_url(placement_provider, region)
        return self._base_url


class VM:
    # Data fields — populated from fork/get/list/refresh; ``None`` on a bare
    # handle from ``arker.vm(id)`` until you call ``refresh()``. Names mirror
    # the contract ``Vm``.
    vm_id: str | None
    name: str | None
    description: str | None
    state: str | None
    owner_org_id: str | None
    created_at: str | None
    public: bool | None
    region: str | None
    provider: str | None
    vcpu_count: int | None
    memory_mib: int | None
    disk_mib: int | None
    network: VmNetwork | None
    resources: VmResources | None
    max_vcpus: int | None
    max_memory_mib: int | None
    min_memory_mib: int | None
    last_active_at: str | None
    sessions: list[Session] | None

    def __init__(self, client: Arker, vm_id: str, base_url: str | None = None, data: Vm | None = None) -> None:
        self._client = client
        self.id = vm_id
        self.base_url = base_url or client._base_url_for(vm_id)
        for f in dataclasses.fields(Vm):
            setattr(self, f.name, getattr(data, f.name) if data is not None else None)
        resources = data.resources if data is not None else None
        self.vcpu_count = resources.vcpu if resources is not None else None
        self.memory_mib = resources.memory_mib if resources is not None else None
        self.disk_mib = resources.disk_mib if resources is not None else None

    def __repr__(self) -> str:
        return f"VM(id={self.id!r}, name={self.name!r}, state={self.state!r})"

    def refresh(self) -> VM:
        """Re-fetch this VM and return a fresh, fully-populated handle."""
        info = _vm_info(self._client._request("GET", _vm_path(self.id), base_url=self.base_url))
        return VM(self._client, self.id, self.base_url, info)

    def fork(self, **kwargs: Any) -> VM:
        """Fork this VM and return its child."""
        return self._client.fork(source_vm_id=self.id, **kwargs)

    def run(
        self,
        command: str,
        *,
        session_id: str | None = None,
        session_idx: int | None = None,
        timeout: int | None = None,
        time_to_background: int | None = None,
        queueing_timeout: int | None = None,
        end_symbol: str | None = None,
        vcpu_count: int | None = None,
        memory_mib: int | None = None,
        disk_mib: int | None = None,
        policies: PolicyDoc | dict[str, Any] | None = None,
        acquire: str | list[str] | None = None,
        release: str | list[str] | None = None,
        signal: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunResult:
        """Run ``command`` in this VM via ``POST /v1/vms/{id}/runs``.

        Synchronous by default. If the run outlives the server sync window
        (``time_to_background``) the API returns a background ack with a
        ``run_id``; run() then transparently polls :meth:`get_run` until the run
        reaches a terminal state and returns the completed
        :class:`CompletedRunResult` — so a synchronous caller always receives
        the final result. Polling is bounded by ``timeout`` (the run's kill
        bound) plus a margin; if that budget is exceeded run() raises an
        :class:`ArkerError` with code ``"timeout"`` (the run keeps executing
        server-side — poll :meth:`get_run` to retrieve it). With no ``timeout``
        the run is unbounded server-side and the poll is unbounded with it.

        Pass ``time_to_background=0`` to skip the wait entirely: run() returns
        the running :class:`BackgroundRunResult` immediately and you manage
        polling yourself via :meth:`get_run`.

        Output is available WHILE a run is still going: poll :meth:`get_run` on
        a ``running`` run and its ``stdout`` grows as the command writes, so a
        long task can be followed live instead of read only at the end.

        Arker's run interface works like a terminal. Sessions are tabs: each
        keeps its own state — working directory, environment, shell history —
        and each handles one run at a time. Create one with
        :meth:`create_session`, which also sets its starting ``cwd`` and
        ``env``, then pass its id::

            server = vm.create_session()
            vm.run("nginx -g 'daemon off;'", time_to_background=0,
                   session_id=server.session_id)
            vm.run("echo hello")   # the default session — the server is untouched

        Run sequential commands in one session; for a long-running task, start
        it with ``time_to_background=0`` in a session of its own so later work does
        not interrupt it. Distinct sessions run concurrently. Omitting
        ``session_id`` uses the VM's default session, which every caller that
        omits it shares.

        ``timeout`` is the execution/kill bound in seconds: the maximum
        wall-clock time the command may run before the host kills it. ``None``
        and ``0`` both mean unbounded. It does not control the synchronous wait;
        use ``time_to_background`` for that.

        ``time_to_background`` is the HTTP sync window in seconds: how long the call
        blocks inline before backgrounding the run and returning a pollable
        ``run_id``. ``None`` (default) = 300, matching the server's
        ``DEFAULT_TIME_TO_BACKGROUND``. It does not bound command
        runtime — that is ``timeout``.

        ``exit_code`` is ``None`` when a prompt ends the run before a command
        completion marker is received. This is expected for ``end_symbol`` and
        REPL commands. If it is unexpected, inspect ``stdout``, exit the active
        interpreter, pass ``end_symbol="none"``, or use another session.

        ``stdout``/``stderr`` are decoded text; ``stdout_bytes``/``stderr_bytes``
        carry the raw bytes for output that is not valid UTF-8 (the service
        base64-encodes those and this SDK decodes them for you). ``stderr`` is
        the program's own error output; ``fail_reason`` is the platform
        explaining a ``state == "failed"`` run, and the two are not the same.
        ``queueing_timeout`` (seconds) queues instead of failing fast: retries
        until the window elapses, then surfaces the error. ``None``/``0`` =
        fail fast.
        """
        policy_doc = (
            policies
            if isinstance(policies, PolicyDoc)
            else _decode_model(PolicyDoc, policies)
            if policies is not None
            else None
        )
        body = RunRequest(
            command=command,
            session_id=session_id,
            session_idx=session_idx,
            timeout=timeout,
            time_to_background=time_to_background,
            queueing_timeout=queueing_timeout,
            end_symbol=end_symbol,
            vcpu_count=vcpu_count,
            memory_mib=memory_mib,
            disk_mib=disk_mib,
            acquire=",".join(acquire) if isinstance(acquire, list) else acquire,
            release=",".join(release) if isinstance(release, list) else release,
            signal=signal,
            policies=policy_doc,
        )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        result = _run_response(self._client._request(
            "POST",
            f"{_vm_path(self.id)}/runs",
            body,
            base_url=self.base_url,
            extra_headers=headers,
            max_queueing_s=queueing_timeout,
        ))
        # The server backgrounds a run that outlived its sync window. When the
        # caller did NOT request background, poll get_run() to a terminal state
        # and hand back the completed run so the synchronous call is
        # transparent. Explicit zero is a pure pass-through — return the ack.
        if isinstance(result, BackgroundRunResult) and time_to_background != 0:
            return self._await_run(result.run_id, timeout)
        return result

    def _await_run(self, run_id: str, timeout: int | None) -> CompletedRunResult:
        """Poll :meth:`get_run` until the run reaches a terminal state, then
        return it as a :class:`CompletedRunResult`. Backs the transparent
        synchronous :meth:`run`: invoked only when the server backgrounds a run
        that outlived its sync window.

        Bounded by ``timeout`` (the run's kill bound) plus a margin, so the
        poll outlives the server-side kill and reports its outcome. An unset or
        ``0`` timeout is unbounded server-side, so the poll is unbounded too —
        giving up at a client-side deadline the caller never asked for would
        abandon a run that is still going."""
        budget_s = run_poll_budget_s(timeout)
        deadline = None if budget_s is None else time.monotonic() + budget_s
        delay = RUN_POLL_INITIAL_S
        consecutive_failures = 0
        while True:
            time.sleep(delay)
            try:
                run = self.get_run(run_id)
            except ArkerError as e:
                consecutive_failures += 1
                if consecutive_failures >= RUN_POLL_MAX_CONSECUTIVE_FAILURES:
                    raise ArkerError(
                        "unavailable",
                        f"run {run_id}: {consecutive_failures} consecutive poll "
                        f"failures (last: {e.code}); the run may still be going "
                        f'server-side — poll get_run("{run_id}") to retrieve it',
                        0,
                    ) from e
                delay = min(RUN_POLL_MAX_S, delay * RUN_POLL_BACKOFF)
                continue
            consecutive_failures = 0
            if run.state in TERMINAL_RUN_STATES:
                return _run_to_completed_result(run)
            if deadline is not None and time.monotonic() >= deadline:
                raise ArkerError(
                    "timeout",
                    f"run {run_id} did not reach a terminal state within "
                    f'{int(budget_s)}s; it continues server-side — poll '
                    f'get_run("{run_id}") to retrieve it',
                    0,
                )
            delay = min(RUN_POLL_MAX_S, delay * RUN_POLL_BACKOFF)

    def update(
        self,
        *,
        vcpu_count: int | None = None,
        memory_mib: int | None = None,
        disk_mib: int | None = None,
        description: str | None | _UnsetType = _UNSET,
        ssh_public_keys: list[str] | None = None,
    ) -> Vm:
        """Update this VM's description, resource allocation, and/or authorized
        SSH keys via ``PATCH /v1/vms/{id}``.
        Pass an empty ``ssh_public_keys`` list to remove all authorized keys.
        Pass ``None`` or an empty description to clear it. Omit
        ``description`` to leave it unchanged. Returns the updated :class:`Vm`."""
        resources: ResourcesInput | None = None
        if vcpu_count is not None or memory_mib is not None or disk_mib is not None:
            resources = ResourcesInput(
                vcpu=vcpu_count,
                memory_mib=memory_mib,
                disk_mib=disk_mib,
            )
        body: PatchVmRequest | dict[str, Any]
        if description is _UNSET:
            body = PatchVmRequest(resources=resources, ssh_public_keys=ssh_public_keys)
        else:
            body = {
                "description": _EXPLICIT_NULL if description is None else description,
                "resources": resources,
                "ssh_public_keys": ssh_public_keys,
            }
        payload = self._client._request("PATCH", _vm_path(self.id), body, base_url=self.base_url)
        return _vm_info(payload)

    def delete(self) -> DeleteVmResponse:
        payload = self._client._request("DELETE", _vm_path(self.id), base_url=self.base_url)
        return _decode_model(DeleteVmResponse, payload)

    def get_policies(self) -> PolicyDoc:
        """Read this VM's network policy via ``GET /v1/vms/{id}/policies``."""
        payload = self._client._request("GET", f"{_vm_path(self.id)}/policies", base_url=self.base_url)
        return _decode_model(PolicyDoc, payload)

    def set_policies(self, doc: PolicyDoc | dict[str, Any]) -> PolicyDoc:
        """Replace this VM's network policy with ``doc`` via
        ``PUT /v1/vms/{id}/policies``. An empty
        doc (``{}`` or ``{"policies": []}``) clears the policy to allow-all.

        Returns the stored policy document, including response-only hostname
        and warning fields::

            vm.set_policies({
                "policies": [
                    {"type": "outbound",
                     "match": {"hosts": ["github.com"], "ports": [443]},
                     "action": "allow"},
                    {"type": "outbound", "action": "deny"},
                ],
            })
        """
        request = doc if isinstance(doc, PolicyDoc) else _decode_model(PolicyDoc, doc)
        payload = self._client._request("PUT", f"{_vm_path(self.id)}/policies", request, base_url=self.base_url)
        return _decode_model(PolicyDoc, payload)

    def sync(
        self,
        path: str,
        data: bytes | str | None = None,
        *,
        from_local: str | None = None,
        to_local: str | None = None,
        cache: dict[str, tuple[int, int, str]] | None = None,
        assume_empty: bool = False,
    ) -> bytes | SyncResult | None:
        """Move files between this VM and the caller.

        One call covers every case; the SDK picks how the bytes travel::

            data = vm.sync("/home/user/out.txt")                     # read a file
            vm.sync("/home/user/in.txt", "hello\\n")                  # write a file
            vm.sync("/home/user/project", from_local="./project")    # upload a file or directory
            vm.sync("/home/user/project", to_local="./project")      # download a file or directory

        ``from_local`` uploads a local path INTO the VM at ``path``.
        Directories are uploaded recursively and incrementally: only files
        that are new or changed on the VM are transferred, and the VM's
        current contents are authoritative, so a repeat call moves nothing.
        File mode (including the executable bit) is preserved. It works on a
        VM that has never run.

        ``to_local`` copies ``path`` OUT of the VM onto the local filesystem,
        recursing when ``path`` is a directory.

        To mount a standalone filesystem into the VM, use
        :meth:`create_sync`.
        """
        if from_local is not None and to_local is not None:
            raise ArkerError("invalid_request", "sync takes from_local or to_local, not both", 0)
        if from_local is not None:
            return self._upload_local(from_local, path, cache=cache, assume_empty=assume_empty)
        if to_local is not None:
            return self._download_to_local(path, to_local)
        if data is None:
            return self._sync_read(path)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._sync_write_stream(path, payload)
        return None

    def _upload_local(
        self,
        local_path: str,
        remote_path: str,
        *,
        cache: dict[str, tuple[int, int, str]] | None = None,
        assume_empty: bool = False,
    ) -> SyncResult:
        """Upload a local file or directory into the VM at ``remote_path``."""
        if os.path.isdir(local_path):
            return self._upload_tree(
                local_path, remote_path, cache=cache, assume_empty=assume_empty
            )

        # A lone file: the destination is the file itself, so an archive is
        # rooted at its parent directory with the basename as its one entry.
        st = os.stat(local_path)
        abs_path = os.path.abspath(local_path)
        remote_norm = "/" + remote_path.lstrip("/")
        remote_root = os.path.dirname(remote_norm) or "/"
        rel = os.path.basename(remote_norm.rstrip("/"))
        entry = (rel, abs_path, st.st_size, st.st_mode)
        kind, compress = self._select_write_transport([entry])
        if kind == "bytes":
            with open(abs_path, "rb") as fh:
                self._sync_write_stream(remote_norm, fh.read())
        else:
            self._upload_and_extract_tarball([(rel, abs_path)], remote_root, compress)
        return SyncResult(sent=1, skipped=0, bytes_sent=st.st_size)

    @staticmethod
    def _select_write_transport(
        files: list[tuple[str, str, int, int]],
    ) -> tuple[str, bool]:
        """Choose how a set of local files is put on the wire.

        Every upload goes through here, so the choice is made in exactly one
        place. A lone small file travels as its own bytes; anything else
        travels as one archive, which keeps a whole tree to a single request
        and carries mode bits with it. Compression is applied only when a
        sample of the payload says it actually compresses.
        """
        if len(files) == 1:
            _rel, _abs, size, mode = files[0]
            if size < ARCHIVE_MIN_BYTES and not mode & 0o111:
                return "bytes", False
        return "archive", VM._should_compress([(rel, abs_path) for rel, abs_path, _s, _m in files])

    @staticmethod
    def _batch_by_size(
        files: list[tuple[str, str, int, int]], cap: int
    ) -> list[list[tuple[str, str, int, int]]]:
        """Split ``files`` into order-preserving groups whose summed size stays
        at or under ``cap`` -- so one archive's upload never trips arkerd's
        per-request ``MAX_SYNC_FILE_BYTES`` cap just because the CHANGED set
        (not any one file) is large.

        Greedy: a batch fills up to (not over) ``cap``, then starts the next.
        A single file at or above ``cap`` gets a batch of its own rather than
        being split -- splitting one file's bytes across archives is a
        separate, unrelated problem this does not attempt to solve; such a
        file still needs to individually clear the server's own per-file size
        limit, exactly as before this batching existed.
        """
        batches: list[list[tuple[str, str, int, int]]] = []
        current: list[tuple[str, str, int, int]] = []
        current_bytes = 0
        for entry in files:
            size = entry[2]
            if current and current_bytes + size > cap:
                batches.append(current)
                current = []
                current_bytes = 0
            current.append(entry)
            current_bytes += size
        if current:
            batches.append(current)
        return batches

    def _download_to_local(self, remote_path: str, local_path: str) -> SyncResult:
        """Copy ``remote_path`` out of the VM to ``local_path``, recursing when
        it is a directory."""
        remote_root = "/" + remote_path.strip("/")
        # A directory listing on a plain file (or a path that does not exist)
        # comes back empty, which is the signal to treat the path as a single
        # file. A genuinely missing path then surfaces its own not-found error
        # from the read.
        try:
            entries, _truncated = self._remote_manifest(remote_root)
        except ArkerError:
            entries = {}

        if not entries:
            payload = self._sync_read(remote_root)
            dest = os.path.abspath(local_path)
            if local_path.endswith(("/", os.sep)) or os.path.isdir(dest):
                dest = os.path.join(dest, os.path.basename(remote_root))
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(payload)
            return SyncResult(sent=1, skipped=0, bytes_sent=len(payload))

        # One request per file: there is no bulk read, so fetch with bounded
        # concurrency to keep the round trips overlapping.
        local_root = os.path.abspath(local_path)
        rels = sorted(entries)
        result = SyncResult()

        def fetch(rel: str) -> int:
            payload = self._sync_read(f"{remote_root}/{rel}")
            dest = os.path.join(local_root, *rel.split("/"))
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(payload)
            return len(payload)

        if len(rels) > 1:
            with ThreadPoolExecutor(max_workers=DOWNLOAD_CONCURRENCY) as pool:
                sizes = list(pool.map(fetch, rels))
        else:
            sizes = [fetch(rel) for rel in rels]
        result.sent = len(sizes)
        result.bytes_sent = sum(sizes)
        return result

    def _sync_upload(
        self,
        params: dict[str, str],
        body: Callable[[], bytes],
        what: str,
    ) -> None:
        """The single upload call site.

        Every write — a lone file and a whole tree alike — goes through here,
        so auth, content type, retry and error handling cannot drift apart
        between them. ``body`` is a factory, not a value, so a retried attempt
        gets fresh bytes.

        The server selects JSON/base64 or raw bytes by ``Content-Type``;
        callers of ``sync()`` never choose a route.
        """
        url = f"{self.base_url}{_vm_path(self.id)}/sync"
        headers = {
            "authorization": f"Bearer {self._client._api_key}",
            "content-type": "application/octet-stream",
        }
        for attempt in range(self._client._retry.attempts):
            try:
                response = _http_client.post(
                    url, params=params, content=body(),
                    headers=headers, timeout=PRESIGNED_PUT_TIMEOUT_S,
                )
            except httpx.RequestError as error:
                if attempt == self._client._retry.attempts - 1:
                    raise ArkerError("unavailable", f"{what} failed: {error}", 0) from error
                time.sleep(self._client._retry_delay(attempt))
                continue
            if response.status_code < 400:
                return
            status = response.status_code
            code, message = "internal", f"{what} failed ({status})"
            try:
                error_body = response.json().get("error") or {}
                code = error_body.get("code") or code
                message = error_body.get("message") or message
            except Exception:
                pass
            # 413 means the body exceeded the accepted size, not a transient
            # fault, so never retry it.
            if status not in RETRYABLE_HTTP or attempt == self._client._retry.attempts - 1:
                raise ArkerError(code, message, status)
            time.sleep(self._client._retry_delay(attempt))

    def _sync_write_stream(self, path: str, data: bytes, sha256: str | None = None) -> None:
        params = {"path": path, "size": str(len(data))}
        if sha256:
            params["sha256"] = sha256
        self._sync_upload(params, lambda: data, "sync write")

    def _sync_extract_file(self, tar_path: str, remote_root: str, mode: str) -> None:
        """Upload an archive straight off disk, so a 2 GB tree does not mean a
        2 GB allocation.

        ``size`` comes from stat, not from a buffered length: a chunked request
        carries no content-length, and the declared size must be exact.
        """
        size = os.path.getsize(tar_path)

        def chunks():
            with open(tar_path, "rb") as fh:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        return
                    yield block

        self._sync_upload(
            {"path": remote_root, "size": str(size), "extract": mode},
            chunks,
            "sync upload",
        )

    def _sync_read(self, path: str) -> bytes:
        request = SyncReadOperationRequest(op="read", path=path)
        payload = self._client._request(
            "POST", f"{_vm_path(self.id)}/sync",
            request, base_url=self.base_url,
        )
        response = _decode_value(SyncReadResponse, payload)
        if isinstance(response, SyncReadInlineResponse):
            return _decode_bytes(response.content, response.encoding)
        if not isinstance(response, SyncReadPresignedResponse):
            raise ArkerError("internal", "sync read returned an unrecognized response", 200)
        signed = _http_client.get(response.presigned_url, timeout=300)
        signed.raise_for_status()
        return signed.content

    def _sync_write_inline(self, path: str, data: bytes) -> None:
        """Write ``data`` through the JSON/base64 ``/sync`` fallback.

        Used only when the raw-upload fast path is
        unavailable — an older server, or the route missing entirely. Chunked
        the same way regardless of size: every chunk shares one ``upload_id``,
        and chunks are grouped into as many sequential requests as it takes to
        keep each request's decoded total at or under ``INLINE_WRITE_LIMIT``
        (comfortably inside the server's hard `MAX_SYNC_INLINE_REQUEST_BYTES`
        cap). arkerd's chunk-session ledger is keyed by (vm, upload_id) and
        outlives any single request, so this reassembles correctly server-side
        — a large inline write costs more round trips, not a hard failure.
        """
        upload_id = _ulid()
        # `or [0]`: an empty file still needs its one (empty) chunk — zero
        # chunks would send `writes: []` and never create the file.
        starts = list(range(0, len(data), CHUNK_SIZE)) or [0]
        entries = [
            SyncChunkWrite(
                path=path,
                size=len(data),
                upload_id=upload_id,
                content=base64.b64encode(data[start : start + CHUNK_SIZE]).decode("ascii"),
                start=start,
                end=min(start + CHUNK_SIZE, len(data)),
            )
            for start in starts
        ]
        last_result: SyncWriteResult | None = None
        for batch in _inline_write_batches(entries):
            results = self._send_writes(batch)
            # Chunks before the last legitimately report written=False; the
            # final chunk's result (in the final batch) carries file
            # completion.
            last_result = results[-1]
        assert last_result is not None  # `entries` is always non-empty
        _assert_write_complete(last_result, "inline write")

    def _send_one_write(self, entry: SyncWriteEntry) -> SyncWriteResult:
        return self._send_writes([entry])[0]

    def _send_writes(self, entries: list[SyncWriteEntry]) -> list[SyncWriteResult]:
        # Chunk entries share one upload_id, so a retry resends the same byte
        # ranges idempotently — the server deduplicates them.
        last_error: tuple[str, str] | None = None
        for attempt in range(self._client._retry.attempts):
            request = SyncWriteOperationRequest(op="write", writes=entries)
            payload = self._client._request(
                "POST", f"{_vm_path(self.id)}/sync",
                request, base_url=self.base_url,
            )
            response = _decode_model(SyncWriteResponse, payload)
            if len(response.results) != len(entries):
                raise ArkerError("internal", "write response missing results", 200)
            error = next(
                (result.error for result in response.results if result.error is not None),
                None,
            )
            if error is None:
                return response.results
            last_error = (error.code, error.message)
            parsed_error = {"code": error.code, "message": error.message}
            if not _is_retryable(200, parsed_error) or attempt == self._client._retry.attempts - 1:
                break
            time.sleep(self._client._retry_delay(attempt))
        raise ArkerError(
            last_error[0] if last_error else "internal",
            last_error[1] if last_error else "write failed",
            200,
        )

    # ── Directory upload (incremental) ────────────────────────────────
    def sync_dir(
        self,
        local_dir: str,
        remote_dir: str,
        *,
        cache: dict[str, tuple[int, int, str]] | None = None,
    ) -> SyncResult:
        """Deprecated. Use ``vm.sync(remote_dir, from_local=local_dir)``, which
        behaves identically and also handles single files and downloads."""
        return self._upload_tree(local_dir, remote_dir, cache=cache)

    def _upload_tree(
        self,
        local_dir: str,
        remote_dir: str,
        *,
        cache: dict[str, tuple[int, int, str]] | None = None,
        assume_empty: bool = False,
    ) -> SyncResult:
        """Recursively sync a local directory INTO this VM at ``remote_dir``:
        compare the local tree against the VM's current contents and upload
        ONLY the files that are new or changed, applied to the VM's filesystem
        in one batch. Works on a VM that has never run.

        The VM's current file state is authoritative. ``cache`` (an optional
        dict you own and reuse across calls) is a pure accelerator: it skips
        re-hashing local files whose (size, mtime) are unchanged. It never
        decides remote state, so it can never cause a stale or missing upload —
        worst case it hashes a file it didn't need to.
        """
        local_root = os.path.abspath(local_dir)
        remote_root = "/" + remote_dir.strip("/")

        # 1. Authoritative remote file listing: rel_path -> content hash. A
        #    directory that doesn't exist yet (or an empty VM) yields {} ->
        #    everything is sent.
        if assume_empty:
            remote, manifest_truncated = {}, False
        else:
            remote, manifest_truncated = self._remote_manifest(remote_root)

        # 2. Enumerate local regular files (skip symlinks — the remote listing
        #    has regular files only, so a symlink would always look "missing").
        local_files: dict[str, tuple[str, int, int, int]] = {}
        for root, _dirs, files in os.walk(local_root):
            for name in files:
                abs_path = os.path.join(root, name)
                if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                    continue
                rel = os.path.relpath(abs_path, local_root).replace(os.sep, "/")
                st = os.stat(abs_path)
                local_files[rel] = (abs_path, st.st_size, st.st_mtime_ns, st.st_mode)

        # 3. Diff local against what the VM has → the set of new/changed files.
        result = SyncResult(manifest_truncated=manifest_truncated)
        changed: list[tuple[str, str, int, int]] = []  # (rel, abs_path, size, mode)
        entries = sorted(local_files.items())
        # Hash in a thread pool. Unlike Node — where JS is single-threaded and
        # hash.update() blocks — CPython's hashlib RELEASES THE GIL while
        # digesting, so this genuinely spreads SHA-256 across cores rather than
        # only overlapping I/O. Hashing dominates a large tree.
        if len(entries) > 1:
            with ThreadPoolExecutor(max_workers=HASH_CONCURRENCY) as pool:
                hashes = list(pool.map(
                    lambda item: _file_hash_cached(item[1][0], item[1][1], item[1][2], cache),
                    entries,
                ))
        else:
            hashes = [
                _file_hash_cached(abs_path, size, mtime_ns, cache)
                for _rel, (abs_path, size, mtime_ns, _mode) in entries
            ]

        # Diff in sorted order so the payload is reproducible and the counters
        # are deterministic regardless of which hash finished first.
        for (rel, (abs_path, size, _mtime_ns, mode)), local_hash in zip(entries, hashes):
            if remote.get(rel) == local_hash:
                result.skipped += 1
                continue
            changed.append((rel, abs_path, size, mode))
            result.sent += 1
            result.bytes_sent += size

        # 4. Upload the changed files and apply them to the VM — far faster
        #    than one write per file. The VM performs the writes, so they are
        #    always consistent with its own filesystem. The outcome is
        #    checked, so a failure surfaces (never a silent partial); the diff
        #    also fails safe: any omitted file is re-sent next call.
        #
        #    One archive per call used to be unconditional, so a changed set
        #    above arkerd's MAX_SYNC_FILE_BYTES (2 GiB) — trivially reached by
        #    a cold sync of a tens-of-GB tree, or a warm sync that happens to
        #    touch that much — failed the whole sync with 413
        #    payload_too_large instead of merely being slow. Splitting into
        #    ARCHIVE_BATCH_MAX_BYTES-sized archives (bounded-concurrent: each
        #    batch extracts a disjoint file set under the same remote_root, so
        #    concurrent `tar -x` calls never race) fixes that without giving
        #    up the one-request-per-batch win for the common case that still
        #    fits in one.
        if changed:
            kind, compress = self._select_write_transport(changed)
            if kind == "bytes":
                rel, abs_path, _size, _mode = changed[0]
                with open(abs_path, "rb") as fh:
                    self._sync_write_stream(f"{remote_root}/{rel}", fh.read())
            else:
                batches = self._batch_by_size(changed, ARCHIVE_BATCH_MAX_BYTES)
                if len(batches) == 1:
                    self._upload_and_extract_tarball(
                        [(rel, abs_path) for rel, abs_path, _s, _m in batches[0]],
                        remote_root, compress,
                    )
                else:
                    with ThreadPoolExecutor(max_workers=ARCHIVE_BATCH_CONCURRENCY) as pool:
                        futures = [
                            pool.submit(
                                self._upload_and_extract_tarball,
                                [(rel, abs_path) for rel, abs_path, _s, _m in batch],
                                remote_root, compress,
                            )
                            for batch in batches
                        ]
                        # Wait for every batch and surface the first failure —
                        # a batch that never ran must not look like success.
                        for future in futures:
                            future.result()
        return result

    def _remote_manifest(self, path: str) -> tuple[dict[str, str], bool]:
        """Fetch the VM's current file listing under ``path`` → ({rel_path:
        content hash}, truncated). Works on a VM that has never run; a path
        that doesn't exist yet yields an empty listing.

        The listing is capped and reports ``truncated``. Past the cap every
        omitted file looks absent, so the diff marks it changed and re-uploads
        it: correct, but it silently turns an incremental sync into a full one
        on exactly the trees where the increment matters most."""
        request = SyncManifestOperationRequest(op="manifest", path=path)
        payload = self._client._request(
            "POST", f"{_vm_path(self.id)}/sync",
            request, base_url=self.base_url,
        )
        response = _decode_model(SyncManifestResponse, payload)
        return (
            {entry.path: entry.hash for entry in response.entries},
            bool(getattr(response, "truncated", False)),
        )

    @staticmethod
    def _should_compress(changed: list[tuple[str, str]]) -> bool:
        """Decide whether compressing this file set earns its keep.

        Compression costs time at both ends, so it only pays when the payload
        genuinely shrinks. Already-compressed data (images, video, archives,
        binaries) is a pure loss; source trees shrink several-fold and are well
        worth it.

        Samples the head of a handful of files rather than compressing
        everything twice. A sample too small to be meaningful is treated as
        compressible, which is harmless at that size.
        """
        raw = 0
        compressed = 0
        for _rel, abs_path in changed[:8]:
            try:
                with open(abs_path, "rb") as fh:
                    chunk = fh.read(128 * 1024)
            except OSError:
                continue  # unreadable sample; the tar step will surface it
            if not chunk:
                continue
            raw += len(chunk)
            compressed += len(gzip.compress(chunk, COMPRESSION_LEVEL))
        if raw < COMPRESSION_SAMPLE_MIN_BYTES:
            return True
        return compressed / raw < COMPRESSION_WORTH_IT_RATIO

    def _upload_and_extract_tarball(
        self, changed: list[tuple[str, str]], remote_root: str, compress: bool
    ) -> None:
        """Pack the changed files (arcname = path relative to ``remote_root``)
        into ONE archive, upload it in a single write, and unpack it inside the
        VM (preserving mode/exec bits and creating missing parent dirs). The
        outcome is checked so any failure surfaces."""
        mode = "tar.gz" if compress else "tar"
        with tempfile.NamedTemporaryFile(suffix=f".{mode}", delete=False) as tf:
            tar_local = tf.name
        try:
            open_mode = "w:gz" if compress else "w"
            kwargs = {"compresslevel": COMPRESSION_LEVEL} if compress else {}
            with tarfile.open(tar_local, open_mode, **kwargs) as tar:
                for rel, abs_path in changed:
                    tar.add(abs_path, arcname=rel, recursive=False)
            # Preferred path: the whole set travels in ONE request and is
            # unpacked before the response. The body is streamed off disk (so a
            # 2 GB tree is not buffered) and is a factory, so a retry reopens
            # the file — a consumed stream cannot be replayed.
            try:
                self._sync_extract_file(tar_local, remote_root, mode)
                return
            except ArkerError as error:
                if error.code != "not_found":
                    raise  # real failure (auth, path escape, size) must not be masked

            # Compatibility fallback, reached only when the preferred path is
            # not available. It must not route back through self.sync(), which
            # would fail identically.
            remote_tar = f"/tmp/.arker-sync-{_ulid()}.{mode}"
            with open(tar_local, "rb") as fh:
                self._sync_write_inline(remote_tar, fh.read())

            q = shlex.quote
            # `set -e` + explicit rm: any failure exits non-zero; the archive is
            # removed on success. Missing parent dirs are created by mkdir/tar.
            cmd = (
                f"set -e; mkdir -p {q(remote_root)}; "
                f"tar -xf {q(remote_tar)} -C {q(remote_root)}; rm -f {q(remote_tar)}"
            )
            res = self.run(cmd)
            code = getattr(res, "exit_code", None)
            state = getattr(res, "state", None)
            if (code not in (0, None)) or state == "failed":
                stderr = getattr(res, "stderr", b"")
                if isinstance(stderr, (bytes, bytearray)):
                    stderr = stderr.decode("utf-8", "replace")
                raise ArkerError(
                    "internal",
                    f"sync upload failed (exit={code}, state={state}): {stderr[:300]}",
                    200,
                )
        finally:
            try:
                os.unlink(tar_local)
            except OSError:
                pass

    # ── Syncs: bindings of a filesystem into this VM at a path ────────
    def list_syncs(self, *, cursor: str | None = None, limit: int | None = None, filesystem_id: str | None = None) -> ListSyncsResponse:
        parameters = ListSyncsParameters(
            id=self.id,
            cursor=cursor,
            limit=limit,
            filesystem_id=filesystem_id,
        )
        path = _build_query(
            f"{_vm_path(self.id)}/syncs", parameters, path_fields={"id"}
        )
        payload = self._client._request("GET", path, base_url=self.base_url)
        return _decode_model(ListSyncsResponse, payload)

    def create_sync(self, *, filesystem_id: str, path: str | None = None) -> Sync:
        """Bind a filesystem into this VM at ``path``."""
        request = SyncCreateRequest(filesystem_id=filesystem_id, path=path)
        payload = self._client._request(
            "POST", f"{_vm_path(self.id)}/syncs", request, base_url=self.base_url
        )
        return _sync(payload)

    def delete_sync(self, sync_id: str) -> DeleteSyncResponse:
        payload = self._client._request("DELETE", f"{_vm_path(self.id)}/syncs/{_segment(sync_id)}", base_url=self.base_url)
        return _decode_model(DeleteSyncResponse, payload)

    # ── Runs ──────────────────────────────────────────────────────────
    def list_runs(self, *, cursor: str | None = None, limit: int | None = None, state: str | None = None,
                  started_after: str | None = None, started_before: str | None = None, completed_after: str | None = None) -> ListRunsResponse:
        parameters = ListRunsParameters(
            id=self.id,
            cursor=cursor,
            limit=limit,
            state=state,
            started_after=started_after,
            started_before=started_before,
            completed_after=completed_after,
        )
        path = _build_query(
            f"{_vm_path(self.id)}/runs", parameters, path_fields={"id"}
        )
        payload = self._client._request("GET", path, base_url=self.base_url)
        return _decode_model(ListRunsResponse, payload)

    def get_run(self, run_id: str) -> RunRecord:
        """Fetch a past run.

        ``stdout``/``stderr`` are bytes, matching what :meth:`run` returns.
        ``stdout_encoding``/``stderr_encoding`` report how the service encoded
        them on the wire, for callers that need to know.
        """
        wire = _run_status_response(
            self._client._request(
                "GET",
                f"{_vm_path(self.id)}/runs/{_segment(run_id)}",
                base_url=self.base_url,
            )
        )
        return _decode_run_record(wire)

    def cancel_run(self, run_id: str) -> CancelRunResponse:
        payload = self._client._request("DELETE", f"{_vm_path(self.id)}/runs/{_segment(run_id)}", base_url=self.base_url)
        return _decode_model(CancelRunResponse, payload)

    # ── Sessions ──────────────────────────────────────────────────────
    def list_sessions(self, *, cursor: str | None = None, limit: int | None = None, state: str | None = None) -> ListSessionsResponse:
        parameters = ListSessionsParameters(
            id=self.id, cursor=cursor, limit=limit, state=state
        )
        path = _build_query(
            f"{_vm_path(self.id)}/sessions", parameters, path_fields={"id"}
        )
        payload = self._client._request("GET", path, base_url=self.base_url)
        return _decode_model(ListSessionsResponse, payload)

    def create_session(self, *, env: dict[str, str] | None = None, cwd: str | None = None) -> Session:
        request = CreateSessionRequest(env=env, cwd=cwd)
        payload = self._client._request(
            "POST", f"{_vm_path(self.id)}/sessions", request, base_url=self.base_url
        )
        return _session_info(payload)

    def get_session(self, session_id: str) -> Session:
        payload = self._client._request("GET", f"{_vm_path(self.id)}/sessions/{_segment(session_id)}", base_url=self.base_url)
        return _session_info(payload)

    def delete_session(self, session_id: str) -> DeleteSessionResponse:
        payload = self._client._request("DELETE", f"{_vm_path(self.id)}/sessions/{_segment(session_id)}", base_url=self.base_url)
        return _decode_model(DeleteSessionResponse, payload)

    def update_session(
        self,
        session_id: str,
        *,
        cols: int | None = None,
        rows: int | None = None,
        timeout_secs: int | None = None,
    ) -> PatchSessionResponse:
        """Update a session via ``PATCH /v1/vms/{id}/sessions/{sid}``: resize its
        PTY (``cols``/``rows``) and/or set the idle ``timeout_secs``. Works whether
        or not a PTY is currently attached — the REST equivalent of
        :meth:`Pty.resize` (which sends an in-band control frame on the live
        WebSocket).
        """
        request = PatchSessionRequest(
            cols=cols, rows=rows, timeout_secs=timeout_secs
        )
        payload = self._client._request(
            "PATCH",
            f"{_vm_path(self.id)}/sessions/{_segment(session_id)}",
            request,
            base_url=self.base_url,
        )
        return _decode_model(PatchSessionResponse, payload)

    # ── Interactive PTY ────────────────────────────────────────────────
    def connect_pty(
        self,
        *,
        on_data: Callable[[bytes], None] | None = None,
        session_id: str | None = None,
        cols: int | None = None,
        rows: int | None = None,
        command: str | None = None,
        persist: bool | None = None,
        cancel_ttl_secs: int | None = None,
        on_close: Callable[[Pty.CloseEvent], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        use_ticket: bool = True,
        env: dict[str, str] | None = None,
        plain: bool = True,
    ) -> Pty:
        """Open an interactive pseudo-terminal in this VM over a WebSocket.

        Mirrors the TypeScript ``connectPty``. Creates a session if
        ``session_id`` is omitted, mints a browser PTY ticket
        (``POST .../pty-ticket``), then opens the PTY WebSocket against this
        VM's *regional* base url. Server→client binary frames are delivered to
        ``on_data`` from a background reader thread.

        Reconnect/persist: pass an existing ``session_id`` with
        ``persist=True`` (the default backend behavior) to reattach to a
        running shell (scrollback is replayed).

        Requires the ``websocket-client`` package — ``pip install 'arker[pty]'``.
        """
        # Plain-text by default. A PTY inherits its session's environment, so
        # this is set when the session is created — no per-command prefixing and
        # no server-side change. An explicit `env` wins over the plain defaults,
        # so a caller can override just `TERM` and keep the rest.
        session_env: dict[str, str] = {}
        if plain:
            session_env.update(PLAIN_PTY_ENV)
        if env:
            session_env.update(env)

        if session_id:
            sid = session_id
            # Reattaching: the session already exists, so its env is fixed. Say
            # so rather than silently ignoring the argument.
            if env and not plain:
                pass
        else:
            sid = self.create_session(env=session_env or None).session_id

        params: dict[str, Any] = {
            "cols": _clamp_pty_dimension(cols) if cols is not None else None,
            "rows": _clamp_pty_dimension(rows) if rows is not None else None,
            "command": command,
            "persist": persist,
            "cancel_ttl_secs": int(cancel_ttl_secs)
            if cancel_ttl_secs and cancel_ttl_secs > 0
            else None,
        }

        ticket: str | None = None
        headers: dict[str, str] | None = None
        if use_ticket:
            payload = self._client._request(
                "POST",
                f"{_vm_path(self.id)}/sessions/{_segment(sid)}/pty-ticket",
                {},
                base_url=self.base_url,
            )
            response = _decode_model(PtyTicketResponse, payload)
            ticket = response.ticket
        else:
            # Header auth (server-side use): Bearer key on the WS upgrade.
            headers = {"authorization": f"Bearer {self._client._api_key}"}

        ws_params = dict(params)
        if ticket is not None:
            ws_params["ticket"] = ticket
        url = _build_pty_ws_url(self.base_url, self.id, sid, ws_params)

        return Pty(
            session_id=sid,
            url=url,
            headers=headers,
            on_data=on_data,
            on_close=on_close,
            on_error=on_error,
        )


class Pty:
    """An interactive pseudo-terminal connection to a VM over a WebSocket.

    Mirrors the TypeScript ``PtyConnection``. Server→client terminal output is
    delivered to the ``on_data`` callback from a background reader thread; use
    :meth:`send_input` to write stdin, :meth:`resize` to change dimensions,
    :meth:`kill` to destroy the shell, and :meth:`close` to detach.

    Obtain one via :meth:`VM.connect_pty`. Requires the ``websocket-client``
    package (``pip install 'arker[pty]'``).
    """

    @dataclasses.dataclass
    class CloseEvent:
        code: int | None = None
        reason: str | None = None

    def __init__(
        self,
        *,
        session_id: str,
        url: str,
        headers: dict[str, str] | None = None,
        on_data: Callable[[bytes], None] | None = None,
        on_close: Callable[[Pty.CloseEvent], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as error:  # pragma: no cover - import guard
            raise ArkerError(
                "missing_dependency",
                "interactive PTY needs the 'websocket-client' package; "
                "install with: pip install 'arker[pty]'",
                0,
            ) from error

        self.session_id = session_id
        self._data_listeners: list[Callable[[bytes], None]] = []
        self._close_listeners: list[Callable[[Pty.CloseEvent], None]] = []
        self._error_listeners: list[Callable[[Exception], None]] = []
        if on_data is not None:
            self._data_listeners.append(on_data)
        if on_close is not None:
            self._close_listeners.append(on_close)
        if on_error is not None:
            self._error_listeners.append(on_error)

        self._open = threading.Event()
        self._open_error: Exception | None = None
        self._closed = False

        header_list = [f"{k}: {v}" for k, v in (headers or {}).items()]
        self._ws = websocket.WebSocketApp(
            url,
            header=header_list,
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever, name="arker-pty", daemon=True
        )
        self._thread.start()

        if not self._open.wait(timeout=connect_timeout):
            self.close()
            raise ArkerError("unavailable", "PTY WebSocket failed to open (timeout)", 0)
        if self._open_error is not None:
            raise ArkerError("unavailable", f"PTY WebSocket failed to open: {self._open_error}", 0)

    # ── Listener registration ──────────────────────────────────────────
    def on_data(self, listener: Callable[[bytes], None]) -> Callable[[], None]:
        self._data_listeners.append(listener)
        return lambda: self._data_listeners.remove(listener) if listener in self._data_listeners else None

    def on_close(self, listener: Callable[[Pty.CloseEvent], None]) -> Callable[[], None]:
        self._close_listeners.append(listener)
        return lambda: self._close_listeners.remove(listener) if listener in self._close_listeners else None

    def on_error(self, listener: Callable[[Exception], None]) -> Callable[[], None]:
        self._error_listeners.append(listener)
        return lambda: self._error_listeners.remove(listener) if listener in self._error_listeners else None

    # ── I/O ─────────────────────────────────────────────────────────────
    def send_input(self, data: bytes | str) -> None:
        """Write stdin bytes to the terminal (a binary frame)."""
        import websocket  # type: ignore

        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)

    # Alias matching the TS ``send`` surface.
    send = send_input

    def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal (a JSON control frame)."""
        self._send_control({
            "type": "resize",
            "cols": _clamp_pty_dimension(cols),
            "rows": _clamp_pty_dimension(rows),
        })

    def kill(self) -> None:
        """Destroy the shell (a JSON ``kill`` control frame)."""
        self._send_control({"type": "kill"})

    def ping(self) -> None:
        self._send_control({"type": "ping"})

    def close(self, code: int | None = None, reason: str | None = None) -> None:
        """Detach: close the WebSocket. With ``persist`` the shell keeps
        running and can be reattached via the same ``session_id``."""
        try:
            if code is not None:
                self._ws.close(status=code, reason=(reason or "").encode("utf-8"))
            else:
                self._ws.close()
        except Exception:
            pass

    # ── Internals ───────────────────────────────────────────────────────
    def _send_control(self, message: dict[str, Any]) -> None:
        import websocket  # type: ignore

        self._ws.send(json.dumps(message), opcode=websocket.ABNF.OPCODE_TEXT)

    def _handle_open(self, _ws: Any) -> None:
        self._open.set()

    def _handle_message(self, _ws: Any, message: Any) -> None:
        if isinstance(message, str):
            message = message.encode("utf-8")
        for listener in list(self._data_listeners):
            listener(message)

    def _handle_error(self, _ws: Any, error: Any) -> None:
        exc = error if isinstance(error, Exception) else Exception(str(error))
        if not self._open.is_set():
            self._open_error = exc
            self._open.set()
        for listener in list(self._error_listeners):
            listener(exc)

    def _handle_close(self, _ws: Any, code: Any, reason: Any) -> None:
        self._closed = True
        # Unblock the constructor if we closed before opening.
        self._open.set()
        event = Pty.CloseEvent(
            code=int(code) if isinstance(code, int) else None,
            reason=reason if isinstance(reason, str) else None,
        )
        for listener in list(self._close_listeners):
            listener(event)


# ── Helpers ─────────────────────────────────────────────────────────


# Shared HTTP/2 client. One connection pool for the process, so concurrent requests
# multiplex over a single connection per host. HTTP/2 is negotiated via ALPN; hosts
# that don't offer it fall back to HTTP/1.1.
_http_client = httpx.Client(http2=True)
atexit.register(_http_client.close)


def _http(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, bytes]:
    response = _http_client.request(method, url, headers=headers, content=data, timeout=REQUEST_TIMEOUT_S)
    return response.status_code, response.content


def _request_json(
    method: str,
    path: str,
    body: object | None = None,
    *,
    base_url: str,
    retry: RetryOptions,
    api_key: str | None = None,
    extra_headers: dict[str, str] | None = None,
    max_queueing_s: int | None = None,
) -> dict[str, Any]:
    url = base_url + path
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    if extra_headers:
        for key, value in extra_headers.items():
            if value is not None:
                headers[key] = value
    data = None
    payload_dict: Any = None

    if body is not None:
        headers["content-type"] = "application/json"
        payload_dict = _drop_none(body)
        data = json.dumps(payload_dict).encode("utf-8")

    # queueing_timeout swaps the retry budget from attempt count to wall-clock
    # window; retry=False (attempts = 1) still means exactly one request.
    deadline = (
        time.monotonic() + max_queueing_s
        if max_queueing_s and retry.attempts > 1
        else None
    )

    attempt = 0
    while True:
        if deadline is not None and attempt > 0 and isinstance(payload_dict, dict):
            # Retries re-send the remaining window.
            remaining = max(1, math.ceil(deadline - time.monotonic()))
            data = json.dumps({**payload_dict, "queueing_timeout": remaining}).encode("utf-8")
        try:
            status, raw = _http(method, url, headers, data)
        except httpx.RequestError as error:
            delay = _retry_delay(retry, attempt)
            if _can_retry_again(retry, attempt, deadline, delay):
                time.sleep(delay)
                attempt += 1
                continue
            raise ArkerError("unavailable", str(error), 0) from error

        text = raw.decode("utf-8", "replace")
        payload = _parse_json(text)
        parsed_error = _extract_error(payload)

        if _is_retryable(status, parsed_error):
            delay = _retry_delay(retry, attempt, parsed_error)
            if _can_retry_again(retry, attempt, deadline, delay):
                time.sleep(delay)
                attempt += 1
                continue

        if parsed_error:
            raise ArkerError(parsed_error["code"], parsed_error["message"], status)
        if status >= 400:
            raise ArkerError("internal", text[:300] or f"HTTP {status}", status)
        if not isinstance(payload, dict):
            raise ArkerError("internal", "response must be a JSON object", status)
        return payload


def _can_retry_again(
    retry: RetryOptions, attempt: int, deadline: float | None, delay_s: float
) -> bool:
    """No window: the attempt count is the budget. With one: the retry's
    sleep must still land inside the window."""
    if deadline is not None:
        return time.monotonic() + delay_s < deadline
    return attempt < retry.attempts - 1


def _retry_delay(
    retry: RetryOptions, attempt: int, error: dict[str, Any] | None = None
) -> float:
    """Wait before the next attempt.

    The server's hint beats backoff, bounded by an explicitly configured
    max_delay_s — the caller's latency budget outranks the server. The default
    max only shapes backoff; applying it here would neuter real capacity waits.
    """
    jitter_range = max(1, int(retry.jitter_s * 1000) + 1)
    jitter = secrets.randbelow(jitter_range) / 1000.0
    hint = (error or {}).get("retry_after")
    if hint is not None:
        if retry.max_delay_s is not None:
            hint = min(float(hint), retry.max_delay_s)
        return float(hint) + jitter
    max_delay = DEFAULT_RETRY_MAX_DELAY_S if retry.max_delay_s is None else retry.max_delay_s
    # A queueing window leaves the attempt count unbounded; cap the exponent so
    # the doubling stays convertible to float. It is long past max_delay by 32.
    base = min(max_delay, retry.base_delay_s * (2 ** min(attempt, 32)))
    return base + jitter


def _build_query(
    path: str,
    parameters: object,
    *,
    path_fields: set[str] | frozenset[str] = frozenset(),
) -> str:
    values = _drop_none(parameters)
    if not isinstance(values, dict):
        raise TypeError("operation parameters must serialize to an object")
    pairs = [
        (key, str(value))
        for key, value in values.items()
        if key not in path_fields
    ]
    qs = urllib.parse.urlencode(pairs)
    return f"{path}?{qs}" if qs else path


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("base_url must not be empty")
    return normalized


def _normalize_placement_label(name: str, value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", normalized):
        raise ValueError(f"{name} must be a valid DNS label")
    return normalized


def _compute_base_url(provider: str, region: str) -> str:
    """Derive the regional API URL from a provider and region pair."""
    normalized_provider = _normalize_placement_label("provider", provider)
    normalized_region = _normalize_placement_label("region", region)
    placement = f"{normalized_provider}-{normalized_region}"
    if len(placement) > 63:
        raise ValueError("provider and region produce a DNS label longer than 63 characters")
    return f"https://{placement}.arker.ai/api"


def _optional_compute_provider(value: object) -> ComputeProvider | None:
    if not isinstance(value, str):
        return None
    try:
        return _normalize_placement_label("provider", value)
    except ValueError:
        return None


def _normalize_retry(retry: RetryOptions | dict[str, Any] | bool | None) -> RetryOptions:
    if retry is False:
        return RetryOptions(attempts=1, base_delay_s=0, max_delay_s=0, jitter_s=0)
    if isinstance(retry, RetryOptions):
        return retry
    if isinstance(retry, dict):
        return RetryOptions(
            attempts=max(1, int(retry.get("attempts", DEFAULT_RETRY_ATTEMPTS))),
            base_delay_s=max(0.0, float(retry.get("base_delay_s", DEFAULT_RETRY_BASE_DELAY_S))),
            max_delay_s=max(0.0, float(retry["max_delay_s"])) if "max_delay_s" in retry else None,
            jitter_s=max(0.0, float(retry.get("jitter_s", DEFAULT_RETRY_JITTER_S))),
        )
    return RetryOptions()


def _vm_path(vm_id: str) -> str:
    return f"/v1/vms/{_segment(vm_id)}"


def _segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _clamp_pty_dimension(value: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(1000, n))


#: Environment that makes a PTY emit PLAIN TEXT instead of colour, cursor
#: addressing and OSC hyperlinks.
#:
#: Applied by default (see ``VM.connect_pty(plain=...)``). The overwhelmingly
#: common consumer of a PTY here is a program, not a person: escape sequences
#: are noise it has to strip, and stripping them is lossy — `\x1b[2K` plus `\r`
#: means "rewrite this line", so discarding the codes leaves every intermediate
#: frame of a progress bar concatenated. Far better to ask the program not to
#: emit them.
#:
#: ``TERM=dumb`` also suppresses cursor addressing, so a full-screen TUI (vim,
#: an interactive Claude Code session) will refuse or degrade — pass
#: ``plain=False`` when a human is actually watching.
PLAIN_PTY_ENV: dict[str, str] = {
    "TERM": "dumb",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "CLICOLOR": "0",
}


def _build_pty_ws_url(base_url: str, vm_id: str, session_id: str, params: dict[str, Any]) -> str:
    """Build the ``wss://`` PTY URL for ``base_url`` (the VM's regional base)."""
    http_url = f"{_normalize_base_url(base_url)}{_vm_path(vm_id)}/sessions/{_segment(session_id)}/pty"
    parsed = urllib.parse.urlsplit(http_url)
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    else:
        raise ValueError(f"unsupported PTY WebSocket protocol: {parsed.scheme}")
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        pairs.append((key, str(value)))
    query = urllib.parse.urlencode(pairs)
    return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, query, ""))


def _drop_none(value: Any) -> Any:
    if value is _EXPLICIT_NULL:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {
            field.name: getattr(value, field.name) for field in dataclasses.fields(value)
        }
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    return value


def _decode_model(model: type[Model], payload: dict[str, Any]) -> Model:
    """Decode a response model, IGNORING fields this SDK does not know.

    Forward compatibility is the point: adding a field to a response is an
    additive change, and an SDK pinned to an older version must keep working
    against a newer deployment.

    A response missing the fields the model REQUIRES still fails, because the
    dataclass constructor rejects the missing argument. So the rule is "the
    response must carry what we need", not "the response must carry nothing
    else".
    """
    fields = dataclasses.fields(model)
    hints = get_type_hints(model)
    values = {
        field.name: _decode_value(hints[field.name], payload[field.name])
        for field in fields
        if field.name in payload
    }
    return model(**values)


def _decode_value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    if origin is list:
        (item_type,) = get_args(annotation)
        return [_decode_value(item_type, item) for item in value]
    if origin is dict:
        _, value_type = get_args(annotation)
        return {key: _decode_value(value_type, item) for key, item in value.items()}
    if origin is types.UnionType:
        args = get_args(annotation)
        model_types = [
            member
            for member in args
            if isinstance(member, type) and dataclasses.is_dataclass(member)
        ]
        if isinstance(value, dict):
            candidates: list[tuple[tuple[int, int], type[Any]]] = []
            for model_type in model_types:
                fields = dataclasses.fields(model_type)
                field_names = {field.name for field in fields}
                required = {
                    field.name
                    for field in fields
                    if field.default is dataclasses.MISSING
                    and field.default_factory is dataclasses.MISSING
                }
                if required.issubset(value):
                    candidates.append(
                        ((len(required), len(field_names.intersection(value))), model_type)
                    )
            if candidates:
                _, model_type = max(candidates, key=lambda candidate: candidate[0])
                return _decode_model(model_type, value)
        # Optional[list[Dataclass]] / Optional[dict[str, Dataclass]]: the
        # single non-null member is itself a parameterized container, not a
        # bare dataclass type (that case is handled above), so it needs its own
        # branch to avoid a raw passthrough —
        # e.g. PolicyDoc.policies: list[PolicyEntry] | None decoded as
        # plain dicts instead of PolicyEntry instances. Recurse into the
        # single non-null member so its own list/dict branch above can
        # decode each item.
        non_none = [member for member in args if member is not type(None)]
        if len(non_none) == 1 and get_origin(non_none[0]) in (list, dict):
            return _decode_value(non_none[0], value)
        return value
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return _decode_model(annotation, value)
    return value


def _parse_json(text: str) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_error(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    try:
        response = _decode_model(ErrorResponse, payload)
    except (KeyError, TypeError):
        return None
    return {
        "code": response.error.code,
        "message": response.error.message,
        "retry_after": _wire_retry_after(response.error.retry_after),
    }


def _wire_retry_after(value: Any) -> float | None:
    """Seconds the server asked us to wait, or None if it did not say usefully.

    The response decoder passes scalars through without checking them against
    the model, so this is where a non-numeric or non-positive value has to be
    rejected — past here it is a number or absent.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _is_retryable(status: int, error: dict[str, Any] | None) -> bool:
    if status in RETRYABLE_HTTP:
        return True
    if not error:
        return False
    if error["code"] in RETRYABLE_CODES:
        return True
    if error["code"] != "internal":
        return False
    return any(hint in error["message"] for hint in TRANSIENT_HINTS)


def _ulid() -> str:
    raw = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        out.append(ULID_ALPHABET[raw & 31])
        raw >>= 5
    return "".join(reversed(out))


def _decode_bytes(text: str, encoding: str) -> bytes:
    if encoding == "base64":
        return base64.b64decode(text)
    return text.encode("utf-8", "replace")


def _assert_write_complete(result: SyncWriteResult, context: str) -> None:
    if result.complete and result.written:
        return
    raise ArkerError("internal", f"{context} did not complete", 200)


def _inline_write_batches(entries: list[SyncChunkWrite]) -> list[list[SyncChunkWrite]]:
    """Group chunk entries (already ``<= CHUNK_SIZE`` each) into request
    batches whose decoded total stays at or under ``INLINE_WRITE_LIMIT``,
    preserving order. A single small write is always exactly one batch."""
    batches: list[list[SyncChunkWrite]] = []
    batch: list[SyncChunkWrite] = []
    batch_bytes = 0
    for entry in entries:
        entry_bytes = entry.end - entry.start
        if batch and batch_bytes + entry_bytes > INLINE_WRITE_LIMIT:
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(entry)
        batch_bytes += entry_bytes
    if batch:
        batches.append(batch)
    return batches


@dataclasses.dataclass
class SyncResult:
    """Outcome of a :meth:`VM.sync` transfer."""

    sent: int = 0
    """Files transferred."""
    skipped: int = 0
    """Files already up-to-date on the VM (nothing transferred)."""
    bytes_sent: int = 0
    """Total bytes of the transferred files."""
    manifest_truncated: bool = False
    """True when the VM's file listing hit the service's entry cap and was
    truncated. Everything beyond the cap is invisible to the comparison, so it
    is treated as changed and re-uploaded — the sync stays CORRECT but stops
    being incremental. If you see this, split the sync into subdirectories."""


#: Deprecated alias of :class:`SyncResult`.
SyncDirResult = SyncResult


def _file_hash_cached(
    abs_path: str,
    size: int,
    mtime_ns: int,
    cache: dict[str, tuple[int, int, str]] | None,
) -> str:
    """Lowercase-hex sha256 of a file, reusing ``cache`` when (size, mtime) are
    unchanged. The cache is a pure accelerator: on any mismatch (or no cache) the
    file is re-hashed, so a stale cache entry can never cause a wrong upload."""
    if cache is not None:
        cached = cache.get(abs_path)
        if cached is not None and cached[0] == size and cached[1] == mtime_ns:
            return cached[2]
    hasher = hashlib.sha256()
    with open(abs_path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    if cache is not None:
        cache[abs_path] = (size, mtime_ns, digest)
    return digest


def _session_info(payload: dict[str, Any]) -> Session:
    return _decode_model(Session, payload)


def _filesystem(payload: dict[str, Any]) -> Filesystem:
    return _decode_model(Filesystem, payload)


def _sync(payload: dict[str, Any]) -> Sync:
    return _decode_model(Sync, payload)


def _vm_info(payload: dict[str, Any]) -> Vm:
    return _decode_model(Vm, payload)


def _terminal_state(state: str | None, exit_code: int | None) -> str:
    """Terminal state for a finished run.

    A negative ``exit_code`` means no process status was obtained — the run was
    killed or the compute was lost — which is ``"failed"``. Keeps the
    synchronous run result and :meth:`VM.get_run` reporting the same state for
    the same run.
    """
    if exit_code is not None and exit_code < 0:
        return "failed"
    return state or "completed"


def _run_response(payload: dict[str, Any]) -> RunResult:
    response = _decode_value(RunResponse, payload)
    if isinstance(response, CompletedRunResponse):
        return CompletedRunResult(
            stdout=_as_text(_decode_bytes(response.stdout, response.stdout_encoding)),
            session_id=getattr(response, "session_id", None),
            stderr=_as_text(_decode_bytes(response.stderr, response.stderr_encoding)),
            stdout_bytes=_decode_bytes(response.stdout, response.stdout_encoding),
            stderr_bytes=_decode_bytes(response.stderr, response.stderr_encoding),
            exit_code=response.exit_code,
            run_id=response.run_id,
            state=_terminal_state(response.state, response.exit_code),
            fail_reason=_optional_str(payload.get("fail_reason")),
            memory_requested_mib=response.memory_requested_mib,
            memory_achieved_mib=response.memory_achieved_mib,
            memory_partial=bool(response.memory_partial),
        )

    if isinstance(response, BackgroundRunResponse):
        return BackgroundRunResult(
            run_id=response.run_id,
            session_id=getattr(response, "session_id", None),
            state=response.state or "running",
        )

    raise ArkerError("internal", "unrecognized run response shape", 200)


def _run_to_completed_result(run: RunRecord) -> CompletedRunResult:
    """Project a terminal run-status (:class:`Run`) into the
    :class:`CompletedRunResult` a synchronous :meth:`VM.run` resolves to. The
    stored run carries no memory-override fields, so those stay ``None``.

    ``run.stdout``/``run.stderr`` are already bytes — :func:`_run_status_response`
    decodes at the wire boundary — so they pass through untouched here. Decoding
    again would corrupt base64 payloads."""
    exit_code = run.exit_code
    if exit_code is None:
        exit_code = 0 if run.state == "completed" else 1
    return CompletedRunResult(
        stdout=run.stdout,
        stderr=run.stderr,
        stdout_bytes=run.stdout_bytes,
        stderr_bytes=run.stderr_bytes,
        exit_code=exit_code,
        run_id=run.run_id,
        state=run.state,
        fail_reason=run.fail_reason,
    )


def _run_status_response(payload: dict[str, Any]) -> Run:
    return _decode_model(Run, payload)


def _decode_run_record(wire: Run) -> RunRecord:
    """Project a wire run record into :class:`RunRecord`, decoding
    ``stdout``/``stderr`` to bytes."""
    fields = dataclasses.asdict(wire)
    stdout_bytes = _decode_bytes(wire.stdout, wire.stdout_encoding)
    stderr_bytes = _decode_bytes(wire.stderr, wire.stderr_encoding)
    fields["stdout"] = _as_text(stdout_bytes)
    fields["stderr"] = _as_text(stderr_bytes)
    return RunRecord(**fields, stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes)


def _org_runs_response(payload: dict[str, Any]) -> ListOrgRunsResponse:
    return _decode_model(ListOrgRunsResponse, payload)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None
