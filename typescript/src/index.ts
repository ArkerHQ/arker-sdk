/**
 * Arker TypeScript SDK.
 *
 * A small wrapper around the VM API. Configure a region for the standard
 * Arker endpoints, or pass baseUrl directly for internal/dev targets.
 */

import { runPollBudgetMs } from "./internal/run-poll.js";
import type { components, operations } from "./generated/api-types.js";

type ApiSchema<Name extends keyof components["schemas"]> = components["schemas"][Name];
type ApiQuery<Name extends keyof operations> = NonNullable<
  operations[Name]["parameters"]["query"]
>;

export const CHUNK_SIZE = 4 * 1024 * 1024;

/**
 * Size at which the router stops buffering a `/sync-stream` body and forwards
 * it as a stream (a server-side proxy body limit). Measured before
 * that change landed: 64 MiB returned 200, 72 MiB returned 413
 * `payload_too_large`.
 *
 * Every write streams, and the router selects buffered or streamed forwarding.
 */
export const STREAM_MAX_BYTES = 64 * 1024 * 1024;

/**
 * Below this, don't bother sampling for compressibility — the gzip decision
 * costs more than it can save. Above it, `syncDir` samples before choosing
 * `tar` vs `tar.gz` (the guest pays ~3.4x for gunzip over a plain untar, so
 * gzipping an incompressible tree is a pure loss).
 */
const COMPRESSION_SAMPLE_MIN_BYTES = 256 * 1024;

/**
 * How many files to hash at once in `syncDir`. Bounds open file descriptors
 * while still overlapping I/O; the CPU side is serial in Node regardless.
 */
const HASH_CONCURRENCY = 8;

/** Ratio below which gzip is judged worth its extraction cost. */
const COMPRESSION_WORTH_IT_RATIO = 0.9;

/** Bump to invalidate every persisted stat cache after a format change. */
const STAT_CACHE_VERSION = 1;

/**
 * Anything modified within this window of the cache being written is re-hashed
 * rather than trusted.
 *
 * Timestamps do not have infinite resolution: FAT/exFAT stores mtime to 2
 * SECONDS. Without a margin, a file edited shortly after we hashed it can land
 * in the same timestamp bucket as the cache write and look unchanged forever.
 * Git calls these entries "racily clean" and re-hashes them; this is the same
 * defence, sized for the coarsest filesystem we might land on.
 */
const STAT_CACHE_RACE_MARGIN_NS = 2_000_000_000n;

/**
 * Stat signature used to decide "did this file change?" WITHOUT reading it.
 *
 * Deliberately wider than (size, mtime), which is forgeable: `cp -p`, `tar -x`,
 * `rsync --times` and `touch -r` all restore mtime, so a same-size edit could
 * look untouched. `ctime` moves on any inode change and cannot be set from
 * userland; `ino`/`dev` catch a path being replaced by a different file.
 *
 * Every field must match before a cached hash is trusted, which makes the extra
 * fields fail-safe: one that is meaningless on some platform (Windows reports
 * creation time as ctime; `ino` can be 0) simply stays constant and contributes
 * nothing, and one that changes spuriously (overlayfs copy-up rewrites `ino`)
 * only costs a re-hash. Neither can produce a missed upload.
 */
interface StatSignature {
  size: number;
  mtimeNs: string;
  ctimeNs: string;
  ino: string;
  dev: string;
  mode: number;
}

function statSignaturesMatch(a: StatSignature, b: StatSignature): boolean {
  return a.size === b.size && a.mtimeNs === b.mtimeNs && a.ctimeNs === b.ctimeNs
    && a.ino === b.ino && a.dev === b.dev && a.mode === b.mode;
}

interface StatCacheFile {
  version: number;
  writtenNs: string;
  entries: Record<string, StatSignature & { hash: string }>;
}

/**
 * Pull the `{error:{code,message}}` envelope off a failed response.
 * Shared so every transfer path reports failures identically.
 */
/**
 * Where the persisted stat cache lives. Honours XDG on Linux and the platform
 * conventions elsewhere.
 */
async function statCachePath(localRoot: string, remoteRoot: string): Promise<string> {
  const os = await import("node:os");
  const nodePath = await import("node:path");
  const { createHash } = await import("node:crypto");
  const base =
    process.env.ARKER_CACHE_DIR ??
    (process.platform === "darwin"
      ? nodePath.join(os.homedir(), "Library", "Caches")
      : process.platform === "win32"
        ? (process.env.LOCALAPPDATA ?? nodePath.join(os.homedir(), "AppData", "Local"))
        : (process.env.XDG_CACHE_HOME ?? nodePath.join(os.homedir(), ".cache")));
  const key = createHash("sha256").update(`${localRoot}\0${remoteRoot}`).digest("hex").slice(0, 32);
  return nodePath.join(base, "arker", "syncdir", `${key}.json`);
}

/**
 * Load the persisted signatures, dropping any that fall inside the racily-clean
 * window. A cache is an accelerator and must never be able to fail a sync, so
 * every failure here (missing, unreadable, corrupt, wrong version) yields an
 * empty map rather than throwing.
 */
async function loadStatCache(file: string): Promise<Map<string, StatSignature & { hash: string }>> {
  const out = new Map<string, StatSignature & { hash: string }>();
  try {
    const fsp = (await import("node:fs")).promises;
    const parsed = JSON.parse(await fsp.readFile(file, "utf8")) as StatCacheFile;
    if (parsed?.version !== STAT_CACHE_VERSION || !parsed.entries) return out;
    const writtenNs = BigInt(parsed.writtenNs);
    for (const [rel, entry] of Object.entries(parsed.entries)) {
      if (BigInt(entry.mtimeNs) >= writtenNs - STAT_CACHE_RACE_MARGIN_NS) continue;
      out.set(rel, entry);
    }
  } catch {
    // No cache, unreadable, or a format we don't recognise: start cold.
  }
  return out;
}

/** Persist via temp+rename so a concurrent reader never sees a half-written file. */
async function saveStatCache(
  file: string,
  entries: Map<string, StatSignature & { hash: string }>,
): Promise<void> {
  try {
    const fsp = (await import("node:fs")).promises;
    const nodePath = await import("node:path");
    await fsp.mkdir(nodePath.dirname(file), { recursive: true });
    const payload: StatCacheFile = {
      version: STAT_CACHE_VERSION,
      // Wall clock, to be comparable with stat's mtimeNs (ns since epoch).
      writtenNs: String(BigInt(Date.now()) * 1_000_000n),
      entries: Object.fromEntries(entries),
    };
    const tmp = `${file}.${process.pid}.tmp`;
    await fsp.writeFile(tmp, JSON.stringify(payload));
    await fsp.rename(tmp, file);
  } catch {
    // Read-only home, no disk, sandboxed CI: caching is best-effort by design.
  }
}

async function parseErrorResponse(
  res: Response,
  fallbackMessage: string,
): Promise<{ code: string; message: string }> {
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    return {
      code: body?.error?.code ?? "internal",
      message: body?.error?.message ?? fallbackMessage,
    };
  } catch {
    return { code: "internal", message: fallbackMessage };
  }
}

/** Org id for callers that explicitly select an Arker-owned public source. */
export const ARKER_ORG_ID = "ArkerHQ";

const DEFAULT_RETRY_ATTEMPTS = 4;
const DEFAULT_RETRY_BASE_DELAY_MS = 200;
const DEFAULT_RETRY_MAX_DELAY_MS = 2_000;
const DEFAULT_RETRY_JITTER_MS = 50;
// ── Synchronous run() auto-poll ────────────────────────────────────
// When a run outlives its server-side sync window (`time_to_background`),
// the API hands back a background ack carrying a run_id. For a synchronous
// caller (one that did not ask to background) run() then polls getRun()
// under the hood until the run reaches a terminal state and resolves to the
// completed run — so the caller transparently gets the final result.
const RUN_POLL_INITIAL_MS = 500;
const RUN_POLL_MAX_MS = 3_000;
const RUN_POLL_BACKOFF = 1.5;
// An unbounded wait is not an infinite one. What still ends it is the SERVICE
// becoming unreachable: this many CONSECUTIVE failed status checks. Any
// answered check resets the counter (a run reported as still `running` is a
// successful check), so a long-running command and a transient network blip
// both survive; only a service that has stopped responding throws.
const RUN_POLL_MAX_CONSECUTIVE_FAILURES = 10;
// Terminal run states — RunState ("pending" | "running" | "completed" |
// "failed" | "cancelled") minus the two NON-terminal states, "pending" and
// "running". A run is "pending" while it waits behind an earlier run on the
// same session; a poller must keep polling through it. Anything not in this
// set is treated as non-terminal, so an unknown future state degrades to
// "keep polling" rather than a false completion.
const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(["completed", "failed", "cancelled"]);
const RETRYABLE_HTTP = new Set([429, 502, 503, 504]);
const RETRYABLE_CODES: ReadonlySet<ErrorCode> = new Set([
  "unavailable",
  "bad_gateway",
  "stale_route",
  "capacity_unavailable",
]);
const TRANSIENT_HINTS = ["503", "Service Unavailable", "throttle", "SlowDown", "ThrottlingException"];
const ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const DEFAULT_REGION_ENV = "ARKER_REGION";
const DEFAULT_PROVIDER_ENV = "ARKER_PROVIDER";
export type ComputeProvider = string;
const DEFAULT_CONTROL_BASE_URL = "https://arker.ai/api";

type FetchLike = typeof fetch;
type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
type JsonObject = Record<string, unknown>;

interface BufferValue extends Uint8Array {
  toString(encoding?: string): string;
}

interface BufferConstructorLike {
  from(input: Uint8Array | ArrayBuffer | string, encoding?: string): BufferValue;
}

export interface RetryOptions {
  attempts?: number;
  baseDelayMs?: number;
  /** Caps backoff delays; when set explicitly, also caps a server `retry_after` hint. */
  maxDelayMs?: number;
  jitterMs?: number;
}

export interface ArkerOptions {
  apiKey?: string;
  /** Region combined with `provider` to build the compute endpoint. */
  region?: string;
  /** Provider for compute calls. */
  provider?: ComputeProvider;
  /** Override the compute base URL (e.g. for internal / dev targets).
   * If set, `provider` + `region` are ignored for compute. */
  baseUrl?: string;
  /** Override the control-plane URL — the CF Worker that owns
   * administrative endpoints like `GET /v1/vms` (cross-provider list)
   * and `/v1/filesystems`. Default `https://arker.ai/api`. */
  controlBaseUrl?: string;
  fetch?: FetchLike;
  retry?: RetryOptions | false;
}

export type RegionDiscoveryOptions = Pick<
  ArkerOptions,
  "controlBaseUrl" | "fetch" | "retry"
>;

export interface VmPlacement {
  provider: ComputeProvider;
  region: string;
}

// ── State enums ────────────────────────────────────────────────────
export type VmState = ApiSchema<"VmState">;
export type SessionState = ApiSchema<"SessionState">;
export type RunState = ApiSchema<"RunState">;
export type ErrorCode = ApiSchema<"ErrorCode">;
export type RegionPlacement = ApiSchema<"RegionPlacement">;
export type ListRegionsResponse = ApiSchema<"ListRegionsResponse">;
export type WhoamiResponse = ApiSchema<"WhoamiResponse">;

/** Read the public placement catalog without configuring compute or auth. */
export async function discoverRegions(
  opts: RegionDiscoveryOptions = {},
): Promise<ListRegionsResponse> {
  const fetchImpl = opts.fetch ?? globalThis.fetch;
  if (!fetchImpl) throw new Error("fetch is required in this runtime");
  const controlBaseUrl = normalizeBaseUrl(
    opts.controlBaseUrl ?? env("ARKER_CONTROL_BASE_URL") ?? DEFAULT_CONTROL_BASE_URL,
  );
  return requestJson(
    "GET",
    `${controlBaseUrl}/v1/regions`,
    undefined,
    {},
    fetchImpl,
    opts.fetch === undefined,
    normalizeRetry(opts.retry),
  );
}

// ── Core resources ─────────────────────────────────────────────────
export type PolicyDoc = ApiSchema<"PolicyDoc">;
export type PolicyWriteRequest = ApiSchema<"PolicyWriteRequest">;
export type PolicyEntry = ApiSchema<"PolicyEntry">;
export type PolicyMatch = ApiSchema<"PolicyMatch">;
export type PolicyAction = ApiSchema<"PolicyAction">;
export type Rewrite = ApiSchema<"Rewrite">;
/** A `ports` element: a single port (`80`) or an inclusive `[start, end]`
 * range (`[1000, 2000]`). A `ports` list may mix the two. */
export type PortSpec = NonNullable<PolicyMatch["ports"]>[number];
export type ForkRequest = ApiSchema<"ForkRequest">;
export type ForkOptions = ForkRequest & { context?: string };
export type VmResources = ApiSchema<"VmResources">;
export type ResourcesInput = ApiSchema<"ResourcesInput">;
export type VmNetwork = ApiSchema<"VmNetwork">;
export type Session = ApiSchema<"Session">;
export type Vm = ApiSchema<"Vm">;
export type ListVmsResponse = ApiSchema<"ListVmsResponse">;
export type ListSessionsResponse = ApiSchema<"ListSessionsResponse">;
export type DeleteVmResponse = ApiSchema<"DeleteVmResponse">;
export type DeleteSessionResponse = ApiSchema<"DeleteSessionResponse">;

// ── Filesystems ────────────────────────────────────────────────────
export type Filesystem = ApiSchema<"Filesystem">;
export type ListFilesystemsResponse = ApiSchema<"ListFilesystemsResponse">;
export type DeleteFilesystemResponse = ApiSchema<"DeleteFilesystemResponse">;
export type FilesystemCreateRequest = ApiSchema<"FilesystemCreateRequest">;

// ── Syncs ──────────────────────────────────────────────────────────
export type Sync = ApiSchema<"Sync">;
export type ListSyncsResponse = ApiSchema<"ListSyncsResponse">;
export type DeleteSyncResponse = ApiSchema<"DeleteSyncResponse">;
export type SyncCreateRequest = ApiSchema<"SyncCreateRequest">;
export type SyncReadOperationRequest = ApiSchema<"SyncReadOperationRequest">;
export type SyncWriteOperationRequest = ApiSchema<"SyncWriteOperationRequest">;
export type SyncWriteEntry = ApiSchema<"SyncWriteEntry">;
export type SyncChunkWrite = ApiSchema<"SyncChunkWrite">;
export type SyncPresignedWriteRequest = ApiSchema<"SyncPresignedWriteRequest">;
export type SyncPresignedWriteCommit = ApiSchema<"SyncPresignedWriteCommit">;
export type SyncReadResponse = ApiSchema<"SyncReadResponse">;
export type SyncReadInlineResponse = ApiSchema<"SyncReadInlineResponse">;
export type SyncReadPresignedResponse = ApiSchema<"SyncReadPresignedResponse">;
export type SyncWriteResponse = ApiSchema<"SyncWriteResponse">;
export type SyncWriteResult = ApiSchema<"SyncWriteResult">;
export type SyncChunkWriteResult = ApiSchema<"SyncChunkWriteResult">;
export type SyncPresignedWriteRequestResult = ApiSchema<"SyncPresignedWriteRequestResult">;
export type SyncCommitWriteResult = ApiSchema<"SyncCommitWriteResult">;
export type SyncByteRange = ApiSchema<"SyncByteRange">;
export type SyncEntryError = ApiSchema<"SyncEntryError">;

// ── Runs ───────────────────────────────────────────────────────────
export type RunRequest = ApiSchema<"RunRequest">;
/**
 * POSIX signal deliverable to a session's foreground process group via
 * {@link VM.signal}. Derived from the generated schema so it stays in sync
 * with `openapi.json` rather than drifting as a hand-written union.
 */
export type RunSignal = NonNullable<RunRequest["signal"]>;
export type RunOptions = Partial<Omit<RunRequest, "command">> & {
  /**
   * Optional idempotency key for retrying the run. Sent as the
   * `Idempotency-Key` HTTP header.
   */
  idempotencyKey?: string;
};
export type RunResponse = ApiSchema<"RunResponse">;
export type CompletedRunResponse = ApiSchema<"CompletedRunResponse">;
export type BackgroundRunResponse = ApiSchema<"BackgroundRunResponse">;
/** The run record exactly as it arrives on the wire: `stdout`/`stderr` are
 * strings tagged by `stdout_encoding`/`stderr_encoding` (`utf-8` or `base64`).
 * Mirrors openapi.json exactly — see tests/contract-compat.ts. */
export type Run = ApiSchema<"Run">;

/** A run record with `stdout`/`stderr` decoded to bytes — what `getRun()`
 * returns.
 *
 * The wire model carries them as encoded strings; every output-bearing surface
 * in this SDK hands back bytes. A hand-written result type like
 * `CompletedRunResult`, outside the wire-contract assertions: `Run` stays the
 * contract mirror, this is its decoded projection. */
/** A fetched run. Output is available as text and as exact bytes.
 *
 * `stdout`/`stderr` are decoded text, ready to print or match on.
 * `stdoutBytes`/`stderrBytes` are exactly what the command wrote — use those
 * when the output is not text (an image, an archive, anything binary), because
 * decoding to text replaces undecodable bytes and cannot be undone. */
export type RunRecord = Run & {
  stdoutBytes: Uint8Array;
  stderrBytes: Uint8Array;
};
export type RunSummary = ApiSchema<"RunSummary">;
export type ListRunsResponse = ApiSchema<"ListRunsResponse">;
export type OrgRunListRow = ApiSchema<"OrgRunListRow">;
export type ListOrgRunsResponse = ApiSchema<"ListOrgRunsResponse">;
export type RunListRow = OrgRunListRow;
export type CancelRunResponse = ApiSchema<"CancelRunResponse">;

// ── Sessions ───────────────────────────────────────────────────────
export type CreateSessionRequest = ApiSchema<"CreateSessionRequest">;
export type PatchSessionRequest = ApiSchema<"PatchSessionRequest">;
export type PatchSessionResponse = ApiSchema<"PatchSessionResponse">;
export type PtyTicketResponse = ApiSchema<"PtyTicketResponse">;

// ── Operation query parameters ─────────────────────────────────────
export type ListVmsParameters = ApiQuery<"listVms">;
export type ListOrgRunsParameters = ApiQuery<"listOrgRuns">;
export type ListFilesystemsParameters = ApiQuery<"listFilesystems">;
export type ListSyncsParameters = ApiQuery<"listSyncs">;
export type ListRunsParameters = ApiQuery<"listRuns">;
export type ListSessionsParameters = ApiQuery<"listSessions">;

// ── VM resize (PATCH /v1/vms/{id}) ─────────────────────────────────
export type PatchVmRequest = ApiSchema<"PatchVmRequest">;

// ── Errors ─────────────────────────────────────────────────────────
export type ErrorResponse = ApiSchema<"ErrorResponse">;

// ── Result shapes for the high-level run() helper ──────────────────
export interface CompletedRunResult {
  type: "completed";
  /** The run's own id. Present for executed runs; absent for operation acks. */
  runId?: string;
  /** Lifecycle state — "completed" or "failed". Mirrors the run-status (`Run`) shape. */
  state: string;
  /** Decoded text, ready to print or match on. */
  stdout: string;
  stderr: string;
  /** Exactly what the command wrote — use these when the output is not text. */
  stdoutBytes: Uint8Array;
  stderrBytes: Uint8Array;
  exitCode: number;
  /** The session this run used. A run always occupies exactly one, and it
   * cannot otherwise be learned: `sessionIdx` is find-or-create and the id is
   * assigned server-side. Null for operation acks, which run no command. */
  sessionId?: string | null;
  /** System failure explanation when `state` is "failed". Distinct from
   * `stderr` (the program's own error output); null otherwise. */
  failReason?: string | null;
  /** Requested total memory (MiB), present when the run carried a memory override. */
  memoryRequestedMib?: number | null;
  /** Achieved total memory (MiB) after the run's resize. */
  memoryAchievedMib?: number | null;
  /** True when the runtime could not reach the requested memory target exactly. */
  memoryPartial?: boolean;
}

export interface BackgroundRunResult {
  type: "background";
  runId: string;
  /** The session this run is executing in — how to find a backgrounded
   * process again without guessing the index it landed on. */
  sessionId?: string | null;
  /** Lifecycle state — "running". */
  state: string;
}

/**
 * Result of {@link VM.run}. A synchronous call (`time_to_background` not zero)
 * always resolves to a {@link CompletedRunResult} — if the run outlives its
 * sync window run() polls it to completion under the hood. Only an explicit
 * `time_to_background: 0` yields a {@link BackgroundRunResult} (the running ack,
 * returned immediately for the caller to poll via {@link VM.getRun}).
 */
export type RunResult = CompletedRunResult | BackgroundRunResult;

export type ListOrgRunsOptions = Omit<
  ListOrgRunsParameters,
  "vms" | "actions" | "status" | "status_min" | "status_max"
> & {
  vmIds?: string[];
  actions?: string[];
  status?: string[];
  statusMin?: ListOrgRunsParameters["status_min"];
  statusMax?: ListOrgRunsParameters["status_max"];
};

export type ListVmsOptions = ListVmsParameters;

export type ListFilesystemsOptions = Omit<ListFilesystemsParameters, "name_prefix"> & {
  namePrefix?: ListFilesystemsParameters["name_prefix"];
};

export type ListSyncsOptions = Omit<ListSyncsParameters, "filesystem_id"> & {
  filesystemId?: ListSyncsParameters["filesystem_id"];
};

export type ListRunsOptions = Omit<
  ListRunsParameters,
  "started_after" | "started_before" | "completed_after"
> & {
  startedAfter?: ListRunsParameters["started_after"];
  startedBefore?: ListRunsParameters["started_before"];
  completedAfter?: ListRunsParameters["completed_after"];
};

export type ListSessionsOptions = ListSessionsParameters;

export type PtyInput = string | Uint8Array | ArrayBuffer;

export interface PtyConnectOptions {
  /** Existing session to attach. Omit to create a new session first. */
  sessionId?: string;
  cols?: number;
  rows?: number;
  command?: string;
  /** Defaults to the backend's persistent detach semantics. */
  persist?: boolean;
  /**
   * Auto-cancel the PTY run after this many seconds with no terminal I/O. When
   * the window elapses the server completes the underlying run and DESTROYS the
   * shell (not just a detach) — a reconnect to the same `sessionId` starts
   * fresh. Unset (default) means no auto-cancel.
   */
  cancelTtlSecs?: number;
  /** @internal Test/runtime override for browser-ticket vs Node-header auth. */
  useTicket?: boolean;
  /** @internal Test/runtime override for WebSocket construction. */
  webSocketFactory?: PtyWebSocketFactory;
}

export interface PtyCloseEvent {
  code?: number;
  reason?: string;
}

export interface PtyConnection {
  readonly sessionId: string;
  readonly ready: Promise<void>;
  onData(listener: (data: Uint8Array) => void): () => void;
  onClose(listener: (event: PtyCloseEvent) => void): () => void;
  onError(listener: (error: unknown) => void): () => void;
  send(data: PtyInput): void;
  resize(cols: number, rows: number): void;
  kill(): void;
  close(code?: number, reason?: string): void;
}

interface PtyWebSocketFactoryInit {
  headers?: Record<string, string>;
}

export type PtyWebSocketFactory = (
  url: string,
  init: PtyWebSocketFactoryInit,
) => PtyWebSocketLike | Promise<PtyWebSocketLike>;

interface PtyWebSocketLike {
  binaryType?: string;
  readyState?: number;
  send(data: string | Uint8Array | ArrayBuffer): void;
  close(code?: number, reason?: string): void;
  addEventListener?: (type: string, listener: (event: unknown) => void) => void;
  removeEventListener?: (type: string, listener: (event: unknown) => void) => void;
  on?: (type: string, listener: (...args: unknown[]) => void) => void;
  off?: (type: string, listener: (...args: unknown[]) => void) => void;
}

interface RetryConfig {
  attempts: number;
  baseDelayMs: number;
  maxDelayMs: number;
  jitterMs: number;
  /** Set only when the caller configured maxDelayMs themselves. */
  hintCapMs?: number;
}

interface ParsedError {
  code: string;
  message: string;
  /** Seconds the server asked us to wait, if it said. */
  retryAfterS?: number;
}

export class ArkerError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(`${code}: ${message}`);
    this.name = "ArkerError";
    this.code = code;
    this.status = status;
  }
}

function rejectUnsupportedRunNetworkInputs(request: unknown): void {
  if (request == null || typeof request !== "object") return;
  const input = request as Record<string, unknown>;
  const fields = ["network", "egress"].filter(
    (field) => input[field] !== undefined,
  );
  if (fields.length > 0) {
    throw new ArkerError(
      "bad_request",
      `run ${fields.join("/")} inputs are not supported; use policies`,
      400,
    );
  }
}

export class Arker {
  /** Compute base URL for `provider` + `region` — used for fork/run/
   * per-VM ops. SDK calls go straight to this host, skipping the CF
   * Worker control plane. */
  private readonly computeBaseUrl?: string;
  /** CF Worker control-plane URL — used for cross-cutting admin calls
   * like list-VMs and filesystems. */
  readonly controlBaseUrl: string;
  readonly region?: string;
  readonly provider?: ComputeProvider;
  private readonly apiKey: string;
  private readonly fetchImpl: FetchLike;
  private readonly http2: boolean;
  private readonly retry: RetryConfig;

  constructor(opts: ArkerOptions = {}) {
    const apiKey = opts.apiKey ?? env("ARKER_API_KEY") ?? env("AUTH_KEY");
    const explicitBaseUrl = opts.baseUrl ?? env("ARKER_BASE_URL");
    const rawRegion = opts.region ?? (explicitBaseUrl ? undefined : env(DEFAULT_REGION_ENV));
    const rawProvider = opts.provider ?? (explicitBaseUrl ? undefined : env(DEFAULT_PROVIDER_ENV));
    if (!explicitBaseUrl && Boolean(rawProvider) !== Boolean(rawRegion)) {
      throw new Error("provider and region are required together unless baseUrl is supplied");
    }
    const provider = rawProvider ? normalizePlacementLabel("provider", rawProvider) : undefined;
    const region = rawRegion ? normalizePlacementLabel("region", rawRegion) : undefined;

    const baseUrl = explicitBaseUrl ?? (provider && region ? computeBaseUrl(provider, region) : undefined);
    const controlBaseUrl = opts.controlBaseUrl ?? env("ARKER_CONTROL_BASE_URL") ?? DEFAULT_CONTROL_BASE_URL;

    if (!apiKey) throw new Error("apiKey is required; pass apiKey or set ARKER_API_KEY");
    this.apiKey = apiKey;
    this.computeBaseUrl = baseUrl ? normalizeBaseUrl(baseUrl) : undefined;
    this.controlBaseUrl = normalizeBaseUrl(controlBaseUrl);
    this.region = region;
    this.provider = provider;
    this.fetchImpl = opts.fetch ?? globalThis.fetch;
    // A custom fetch owns transport; otherwise prefer HTTP/2 multiplexing on Node.
    this.http2 = opts.fetch === undefined;
    this.retry = normalizeRetry(opts.retry);

    if (!this.fetchImpl) throw new Error("fetch is required in this runtime");
  }

  get baseUrl(): string {
    if (!this.computeBaseUrl) {
      throw new Error(
        "provider and region or baseUrl are required; pass provider and region, baseUrl, ARKER_PROVIDER and ARKER_REGION, or ARKER_BASE_URL",
      );
    }
    return this.computeBaseUrl;
  }

  /**
   * Address an existing VM. Doesn't make any network calls; returns a
   * lightweight handle.
   */
  vm(vmId: string, placement?: VmPlacement): VM {
    return new VM(this, vmId, this._baseUrlFor(vmId, placement));
  }

  /**
   * Create a VM from one VM, image, or local Dockerfile source.
   *
   *     fork({ source_vm_name: "base" })
   *     fork({ source_vm_id: "vm_abc", layers: ["disk"] })
   *     fork({ image: "ubuntu:24.04" })
   *     fork({ dockerfile: "FROM ubuntu:24.04\nRUN echo ready" })
   *
   * Use {@link VM.fork} when the source is already a VM handle.
   */
  async fork(options: ForkOptions): Promise<VM> {
    const { context, ...request } = options;

    const dockerfile = request.dockerfile;
    if (context !== undefined && typeof dockerfile !== "string") {
      throw new TypeError("context is only valid with dockerfile");
    }
    if (typeof dockerfile === "string") {
      const { dockerfile: _dockerfile, ...forkOptions } = request;
      return this.forkDockerfile(dockerfile, context, forkOptions);
    }
    return this._fork(request as ForkRequest, this.baseUrl);
  }

  /** @internal */
  async _fork(options: ForkRequest, baseUrl: string): Promise<VM> {
    const wire = await this._request<Vm>(
      "POST",
      "/v1/fork",
      options,
      baseUrl,
      undefined,
      options.queueing_timeout,
    );
    return new VM(this, wire.vm_id, baseUrl, wire);
  }

  private async forkDockerfile(
    dockerfile: string,
    context: string | undefined,
    forkOptions: Record<string, unknown>,
  ): Promise<VM> {
    const { applySteps } = await import("./build.js");
    const { DockerfileError, parseDockerfile } = await import("./buildSpec.js");
    const fs = await import("node:fs");
    const nodePath = await import("node:path");

    let path: string | undefined;
    try {
      if (fs.existsSync(dockerfile) && fs.statSync(dockerfile).isFile()) {
        path = nodePath.resolve(dockerfile);
      }
    } catch {
      path = undefined;
    }
    const text = path ? fs.readFileSync(path, "utf8") : dockerfile;

    let parsed;
    try {
      parsed = parseDockerfile(text);
    } catch (error) {
      if (error instanceof DockerfileError) {
        throw new ArkerError("bad_request", `fork: ${error.message}`, 400);
      }
      throw error;
    }

    const contextRoot = context
      ? nodePath.resolve(context)
      : path
        ? nodePath.dirname(path)
        : process.cwd();
    if (!fs.existsSync(contextRoot) || !fs.statSync(contextRoot).isDirectory()) {
      throw new ArkerError("bad_request", `fork: context is not a directory: ${contextRoot}`, 400);
    }

    const vm = await this._fork(
      { ...forkOptions, image: parsed.baseImage } as ForkRequest,
      this.baseUrl,
    );
    await applySteps(vm, parsed.steps, contextRoot);
    return vm;
  }

  async listVms(opts: ListVmsOptions = {}): Promise<{ vms: VM[]; nextCursor: string | null }> {
    const query: ListVmsParameters = opts;
    const resp = await this._request<ListVmsResponse>("GET", buildQuery("/v1/vms", query), undefined, this.controlBaseUrl);
    const vms = (resp.vms ?? []).map((v) => {
      const id = v.vm_id;
      return new VM(this, id, this._baseUrlFor(id, v), v);
    });
    return { vms, nextCursor: resp.next_cursor ?? null };
  }

  /** List available public provider and region placements. */
  async listRegions(): Promise<ListRegionsResponse> {
    return this._request("GET", "/v1/regions", undefined, this.controlBaseUrl);
  }

  /** Return the organization associated with the configured credentials. */
  async whoami(): Promise<WhoamiResponse> {
    return this._request("GET", "/v1/whoami", undefined, this.controlBaseUrl);
  }

  /**
   * List run activity visible to the authenticated caller across VMs,
   * providers, and regions. Admin call — routed through the control plane.
   */
  async listRuns(opts: ListOrgRunsOptions = {}): Promise<ListOrgRunsResponse> {
    const query: ListOrgRunsParameters = {
      since: opts.since,
      until: opts.until,
      vm: opts.vm,
      vms: opts.vmIds && opts.vmIds.length > 0 ? opts.vmIds.join(",") : undefined,
      region: opts.region,
      provider: opts.provider,
      search: opts.search,
      limit: opts.limit,
      offset: opts.offset,
      lite: opts.lite === undefined ? undefined : opts.lite,
      runtime: opts.runtime,
      endpoint: opts.endpoint,
      actions: opts.actions && opts.actions.length > 0 ? opts.actions.join(",") : undefined,
      status: opts.status && opts.status.length > 0 ? opts.status.join(",") : undefined,
      status_min: opts.statusMin,
      status_max: opts.statusMax,
      sort: opts.sort,
      dir: opts.dir,
    };
    return this._request("GET", buildQuery("/v1/runs", query), undefined, this.controlBaseUrl);
  }

  /** Compute call — goes direct to the backend hosting this VM (no
   * control-plane hop). Returns a fully-populated VM handle. */
  async getVm(vmId: string, placement?: VmPlacement): Promise<VM> {
    const baseUrl = this._baseUrlFor(vmId, placement);
    const data = await this._request<Vm>("GET", vmPath(vmId), undefined, baseUrl);
    return new VM(this, vmId, baseUrl, data);
  }

  // ── Filesystems (region-scoped) ────────────────────────────────────
  // Route to the regional endpoint (baseUrl), not the control plane: the
  // control-plane path (arker.ai → api_proxy_bash) does not route
  // /v1/filesystems, while the regional endpoint serves the full CRUD.
  async listFilesystems(opts: ListFilesystemsOptions = {}): Promise<ListFilesystemsResponse> {
    const query: ListFilesystemsParameters = {
      cursor: opts.cursor, limit: opts.limit, name_prefix: opts.namePrefix,
    };
    return this._request("GET", buildQuery("/v1/filesystems", query), undefined, this.baseUrl);
  }

  async createFilesystem(request: FilesystemCreateRequest): Promise<Filesystem> {
    return this._request("POST", "/v1/filesystems", request, this.baseUrl);
  }

  async getFilesystem(filesystemId: string): Promise<Filesystem> {
    return this._request("GET", `/v1/filesystems/${pathSegment(filesystemId)}`, undefined, this.baseUrl);
  }

  async deleteFilesystem(filesystemId: string): Promise<DeleteFilesystemResponse> {
    return this._request("DELETE", `/v1/filesystems/${pathSegment(filesystemId)}`, undefined, this.baseUrl);
  }

  /** @internal */
  async _request<T>(
    method: HttpMethod,
    path: string,
    body?: unknown,
    baseUrl = this.baseUrl,
    extraHeaders?: Record<string, string | undefined>,
    maxQueueingSecs?: number | null,
  ): Promise<T> {
    const url = `${baseUrl}${path}`;
    const headers: Record<string, string> = {
      authorization: `Bearer ${this.apiKey}`,
    };
    if (extraHeaders) {
      for (const [key, value] of Object.entries(extraHeaders)) {
        if (value !== undefined) headers[key] = value;
      }
    }
    return requestJson(
      method,
      url,
      body,
      headers,
      this.fetchImpl,
      this.http2,
      this.retry,
      maxQueueingSecs ?? undefined,
    );
  }

  /** @internal */
  async _fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    return this.fetchImpl(input, init);
  }

  /** @internal */
  _retryAttempts(): number {
    return this.retry.attempts;
  }

  /** @internal */
  _retryDelay(attempt: number, error?: ParsedError): number {
    return retryDelay(this.retry, attempt, error);
  }

  /** @internal */
  _authHeaders(): Record<string, string> {
    return { authorization: `Bearer ${this.apiKey}` };
  }

  /**
   * True when we're on the runtime's own `fetch`, which accepts a ReadableStream
   * request body given `duplex: "half"`. A caller-supplied `fetch` may not, so
   * streamed uploads fall back to a buffered body rather than risk a TypeError
   * deep inside a transfer.
   * @internal
   */
  _supportsStreamingBody(): boolean {
    return this.fetchImpl === globalThis.fetch;
  }

  /** @internal */
  _baseUrlFor(
    ref: string,
    placement?: { provider?: unknown; region?: string | null },
  ): string {
    const provider = optionalComputeProvider(placement?.provider);
    const region = placement?.region?.trim();
    if (provider && region) return computeBaseUrl(provider, region);
    return this.baseUrl;
  }
}

export interface ListOpts {
  cursor?: ListVmsParameters["cursor"];
  limit?: ListVmsParameters["limit"];
}

export class VM {
  readonly id: string;
  readonly baseUrl: string;
  /** @internal */
  readonly _client: Arker;
  // ── Data fields ──────────────────────────────────────────────────
  // Populated from fork/get/list/refresh; `undefined` on a bare handle
  // from `arker.vm(id)` until you call `refresh()`. Names mirror the
  // contract (`Vm`).
  readonly vm_id?: Vm["vm_id"];
  readonly name?: Vm["name"];
  readonly state?: Vm["state"];
  readonly owner_org_id?: Vm["owner_org_id"];
  readonly created_at?: Vm["created_at"];
  readonly public?: Vm["public"];
  readonly region?: Vm["region"];
  readonly provider?: Vm["provider"];
  readonly vcpu_count?: number | null;
  readonly memory_mib?: number | null;
  readonly disk_mib?: number | null;
  readonly network?: Vm["network"];
  readonly resources?: Vm["resources"];
  readonly max_vcpus?: Vm["max_vcpus"];
  readonly max_memory_mib?: Vm["max_memory_mib"];
  readonly min_memory_mib?: Vm["min_memory_mib"];
  readonly last_active_at?: Vm["last_active_at"];
  readonly root_source_vm_id?: Vm["root_source_vm_id"];
  readonly root_source_vm_name?: Vm["root_source_vm_name"];
  readonly worker_id?: string | null;
  readonly sessions?: Vm["sessions"];

  constructor(client: Arker, vmId: string, baseUrl = client._baseUrlFor(vmId), data?: Vm) {
    this._client = client;
    this.id = vmId;
    this.baseUrl = baseUrl;
    if (data) Object.assign(this, data);
    // Keep the client reference off enumeration so `JSON.stringify(vm)` /
    // `console.log(vm)` show just the VM's data, not the whole SDK.
    Object.defineProperty(this, "_client", { enumerable: false });
  }

  /** Re-fetch this VM and return a fresh, fully-populated handle. */
  async refresh(): Promise<VM> {
    const data = await this._client._request<Vm>("GET", vmPath(this.id), undefined, this.baseUrl);
    return new VM(this._client, this.id, this.baseUrl, data);
  }

  /** Fork this VM and return its child. */
  async fork(options: Partial<ForkRequest> = {}): Promise<VM> {
    return this._client._fork(
      { ...options, source_vm_id: this.id } as ForkRequest,
      this.baseUrl,
    );
  }

  /**
   * Run `command` in this VM via `POST /v1/vms/{id}/runs`.
   *
   * Synchronous by default. If the run outlives the server sync window
   * (`time_to_background`, default 120s) the API returns a background ack
   * with a `run_id`; run() then transparently polls {@link getRun} until the
   * run reaches a terminal state and resolves to the completed run — so a
   * synchronous caller always receives the final result. Polling is bounded
   * by the run's `timeout` (its kill bound) plus a margin; if that budget is
   * exceeded run() throws an ArkerError with code `"timeout"` (the run keeps
   * executing server-side — poll {@link getRun} to retrieve it). With no
   * `timeout` the run is unbounded server-side and the poll is unbounded with
   * it.
   *
   * Pass `time_to_background: 0` to skip the wait entirely: run() returns the
   * running acknowledgement (`{ type: "background", runId }`) immediately and
   * you manage polling yourself via {@link getRun}.
   *
   * `queueing_timeout` (seconds) queues instead of failing fast: retries
   * until the window elapses, then surfaces the error. Omitted/`0` = fail fast.
   */
  async run(command: string, options: RunOptions & { time_to_background: 0 }): Promise<BackgroundRunResult>;
  async run(
    command: string,
    options?: Omit<RunOptions, "time_to_background"> & { time_to_background?: null | undefined },
  ): Promise<CompletedRunResult>;
  async run(command: string, options: RunOptions): Promise<RunResult>;
  async run(command: string, options: RunOptions = {}): Promise<RunResult> {
    rejectUnsupportedRunNetworkInputs(options);
    const { idempotencyKey, ...body } = options;
    const headers = idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined;
    const request: RunRequest = { ...body, command };
    const response = await this._client._request<unknown>(
      "POST",
      `${vmPath(this.id)}/runs`,
      request,
      this.baseUrl,
      headers,
      options.queueing_timeout,
    );
    const result = parseRunResponse(response);
    // The server backgrounds a run that outlived its sync window. When the
    // caller did NOT ask to skip the wait, poll getRun() to a terminal state
    // and hand back the completed run so the synchronous call is transparent.
    // Explicit zero is a pure pass-through — return the ack immediately.
    if (result.type === "background" && options.time_to_background !== 0) {
      return this._awaitRun(result.runId, options.timeout);
    }
    return result;
  }

  /**
   * Deliver a POSIX signal to a session's foreground process group.
   *
   * Sent as `POST /v1/vms/{id}/runs` with `signal` and no `command`; the
   * service acknowledges immediately and returns no run id.
   *
   * This is the recovery path when a session is stuck — e.g. an interactive
   * program (`python3`, `psql`, `cat`) holds the terminal and never returns to
   * a prompt the run matcher recognises. Nothing else clears that state:
   * {@link cancelRun} cancels the run record but not the process, a run's
   * `timeout` does not apply once a REPL owns the terminal, and attaching a
   * PTY to a busy session does not get through. A signal request
   * short-circuits before the exec dispatch, so it is not queued behind the
   * stuck run.
   *
   * `SIGKILL` is the reliable choice. `SIGINT` behaves exactly as Ctrl-C would
   * — a Python REPL catches it, prints `KeyboardInterrupt` and keeps running —
   * so it will not free a session held by one.
   *
   * Requires a live runtime: a VM that has been forked but never run throws
   * `not_found` ("no running runtime to signal"), since there is no session
   * whose foreground group could receive it.
   *
   * ```ts
   * await vm.signal("SIGKILL");              // default session
   * await vm.signal("SIGKILL", { sessionIdx: 0 });
   * await vm.run("echo back");               // session is usable again
   * ```
   */
  async signal(
    signal: RunSignal,
    options: { sessionId?: string; sessionIdx?: number } = {},
  ): Promise<CompletedRunResult> {
    const request: RunRequest = {
      signal,
      ...(options.sessionId !== undefined ? { session_id: options.sessionId } : {}),
      ...(options.sessionIdx !== undefined ? { session_idx: options.sessionIdx } : {}),
    } as RunRequest;
    const response = await this._client._request<unknown>(
      "POST",
      `${vmPath(this.id)}/runs`,
      request,
      this.baseUrl,
    );
    const result = parseRunResponse(response);
    if (result.type === "background") {
      // The contract says a signal request never returns a run id; treat a
      // background ack as a protocol violation rather than silently polling.
      throw new ArkerError(
        "unexpected_response",
        `signal ${signal} unexpectedly returned a background run id`,
        502,
      );
    }
    return result;
  }

  /**
   * Poll {@link getRun} until the run reaches a terminal state, then return it
   * as a {@link CompletedRunResult}. Backs the transparent synchronous run():
   * invoked only when the server backgrounds a run that outlived its sync
   * window.
   *
   * Bounded by `timeoutSecs` (the run's kill bound) plus a margin, so the poll
   * outlives the server-side kill and reports its outcome. An unset or `0`
   * timeout is unbounded server-side, so the poll is unbounded too — giving up
   * at a client-side deadline the caller never asked for would abandon a run
   * that is still going.
   */
  private async _awaitRun(runId: string, timeoutSecs?: number | null): Promise<CompletedRunResult> {
    const budgetMs = runPollBudgetMs(timeoutSecs);
    const deadline = budgetMs === null ? null : Date.now() + budgetMs;
    // budgetMs and deadline are null together, so the throw below can read
    // budgetMs without a non-null assertion.
    let delay = RUN_POLL_INITIAL_MS;
    let consecutiveFailures = 0;
    for (;;) {
      await sleep(delay);
      let run: RunRecord;
      try {
        run = await this.getRun(runId);
      } catch (error) {
        consecutiveFailures += 1;
        if (consecutiveFailures >= RUN_POLL_MAX_CONSECUTIVE_FAILURES) {
          const code = error instanceof ArkerError ? error.code : "unavailable";
          throw new ArkerError(
            "unavailable",
            `run ${runId}: ${consecutiveFailures} consecutive poll failures ` +
              `(last: ${code}); the run may still be going server-side — poll ` +
              `getRun(${JSON.stringify(runId)}) to retrieve it`,
            0,
          );
        }
        delay = Math.min(RUN_POLL_MAX_MS, Math.ceil(delay * RUN_POLL_BACKOFF));
        continue;
      }
      consecutiveFailures = 0;
      if (TERMINAL_RUN_STATES.has(run.state)) return runToCompletedResult(run);
      if (budgetMs !== null && deadline !== null && Date.now() >= deadline) {
        throw new ArkerError(
          "timeout",
          `run ${runId} did not reach a terminal state within ${Math.round(budgetMs / 1000)}s; ` +
            `it continues server-side — poll getRun(${JSON.stringify(runId)}) to retrieve it`,
          0,
        );
      }
      delay = Math.min(RUN_POLL_MAX_MS, Math.ceil(delay * RUN_POLL_BACKOFF));
    }
  }

  /**
   * Read or write a file in this VM over `POST /v1/vms/{id}/sync`.
   * Omit `data` to read; pass `data` (string or bytes) to write. The
   * client picks inline transfer for small files and presigned uploads
   * for large ones automatically.
   *
   *     const bytes = await vm.sync("/home/user/out.txt");   // read
   *     await vm.sync("/home/user/in.txt", "hello\n");       // write
   *
   * To mount a standalone filesystem into the VM, use `vm.syncs.create`.
   */
  async sync(path: string): Promise<Uint8Array>;
  async sync(path: string, data: Uint8Array | string): Promise<void>;
  async sync(path: string, data?: Uint8Array | string): Promise<Uint8Array | void> {
    if (data === undefined) return this.syncRead(path);
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    // Stream straight to the VM's disk, at every size. Syncing to a VM has no
    // reason to detour through object storage — the bytes' destination is the
    // guest filesystem, not S3. Presigned uploads existed only because the
    // router buffered proxied bodies and capped them; it now forwards
    // sync-stream as a stream above that cap, so the detour is gone. Presigned
    // remains correct for SHARED FILESYSTEMS, where the bytes really do live in
    // S3 — that is a different route (`/dirs/{id}/sync`), not this one.
    await this.syncWriteStream(path, bytes);
  }

  /**
   * Recursively sync a local directory INTO this VM at `remoteDir`, rsync-style:
   * fetch the VM's file *manifest* (per-file sha256) in ONE request, diff it
   * against the local tree, and upload ONLY the files that are new or changed —
   * packed into a single tarball the guest extracts with `tar -x` (so the guest
   * does the writes, always consistent with its own filesystem). Node-only: it
   * reads the local filesystem.
   *
   * The remote manifest is authoritative. `options.cache` (a caller-owned Map you
   * reuse across calls) is a pure accelerator: it skips re-hashing local files
   * whose (size, mtime) are unchanged. It never decides remote state, so it can
   * never cause a stale or missing upload — worst case it re-hashes a file it
   * didn't need to.
   *
   *     const cache = new Map();
   *     await vm.syncDir("./project", "/home/user/project", { cache }); // full
   *     await vm.syncDir("./project", "/home/user/project", { cache }); // delta
   */
  async syncDir(
    localDir: string,
    remoteDir: string,
    options: SyncDirOptions & { ignore?: (rel: string) => boolean } = {},
  ): Promise<SyncDirResult> {
    const fs = await import("node:fs");
    const nodePath = await import("node:path");
    const { createHash } = await import("node:crypto");
    const fsp = fs.promises;

    const localRoot = nodePath.resolve(localDir);
    const remoteRoot = "/" + remoteDir.replace(/^\/+/, "").replace(/\/+$/, "");

    // 1. Authoritative remote manifest: rel_path -> sha256. A directory that
    //    doesn't exist yet (or an empty VM) yields {} -> everything is sent.
    const clock = () => Number(process.hrtime.bigint() / 1000n) / 1000;
    //    `assumeEmpty` skips the round-trip on a destination the caller knows
    //    is fresh. `manifestMs` then rounds to 0 on its own (the skip costs
    //    microseconds against 0.1ms reporting precision), so a caller can still
    //    tell "skipped" from "merely fast" — a real fetch is ~184ms.
    const tManifest0 = clock();
    const manifest = options.assumeEmpty
      ? { entries: new Map<string, string>(), truncated: false }
      : await this.remoteManifest(remoteRoot);
    const remote = manifest.entries;
    const manifestMs = clock() - tManifest0;
    const tWalk0 = clock();

    // 2. Enumerate local regular files (skip symlinks — the manifest lists
    //    regular files only, so a symlink would always look "missing").
    const localFiles: Array<{ rel: string; abs: string; sig: StatSignature }> = [];
    const walk = async (dir: string): Promise<void> => {
      for (const dirent of await fsp.readdir(dir, { withFileTypes: true })) {
        const abs = nodePath.join(dir, dirent.name);
        if (dirent.isSymbolicLink()) continue;
        if (dirent.isDirectory()) { await walk(abs); continue; }
        if (!dirent.isFile()) continue;
        // bigint stats: nanosecond timestamps, and ino/dev without precision loss.
        const st = await fsp.stat(abs, { bigint: true });
        const rel = nodePath.relative(localRoot, abs).split(nodePath.sep).join("/");
        // Filtered BEFORE hashing, so an ignored file cannot perturb the
        // incremental diff — not merely skipped at upload time.
        if (options.ignore?.(rel)) continue;
        localFiles.push({
          rel, abs,
          sig: {
            size: Number(st.size),
            mtimeNs: String(st.mtimeNs),
            ctimeNs: String(st.ctimeNs),
            ino: String(st.ino),
            dev: String(st.dev),
            mode: Number(st.mode),
          },
        });
      }
    };
    await walk(localRoot);
    localFiles.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
    const walkMs = clock() - tWalk0;
    const tHash0 = clock();

    // 3. Diff local vs the REMOTE manifest -> the set to transfer.
    const cache = options.cache;
    const result: SyncDirResult = { sent: 0, skipped: 0, bytesSent: 0 };
    if (manifest.truncated) result.manifestTruncated = true;
    const changed: Array<{ rel: string; abs: string }> = [];

    // Hash with bounded concurrency, streaming each file rather than reading it
    // whole. Two distinct wins, and it is worth being precise about which:
    //
    //   - Streaming replaces `readFile`, which buffered an ENTIRE file in memory
    //     just to digest it — a 2 GB file meant a 2 GB allocation.
    //   - Concurrency overlaps the file I/O.
    //
    // It does NOT spread SHA-256 across cores: Node runs JS on one thread and
    // `hash.update()` is synchronous, so the CPU half stays serial. Real
    // multi-core hashing here would need worker_threads. (Python's hashlib
    // releases the GIL, so the same change there IS parallel on CPU.)
    const cacheFile = await statCachePath(localRoot, remoteRoot);
    const statCache = options.cache ? new Map() : await loadStatCache(cacheFile);
    const fresh = new Map<string, StatSignature & { hash: string }>();
    const hashes = new Array<string>(localFiles.length);
    let nextIndex = 0;
    const workers = Math.min(HASH_CONCURRENCY, localFiles.length);
    await Promise.all(
      Array.from({ length: workers }, async () => {
        for (;;) {
          const index = nextIndex++;
          const file = localFiles[index];
          if (!file) return;
          // Cheap change detection: if every stat field matches what we
          // recorded, the contents cannot have changed under us, so skip the
          // read entirely. This is the whole point of the cache — an untouched
          // tree is answered from stat alone.
          const persisted = statCache.get(file.rel);
          if (persisted && statSignaturesMatch(persisted, file.sig)) {
            hashes[index] = persisted.hash;
            fresh.set(file.rel, persisted);
            continue;
          }
          const cached = cache?.get(file.abs);
          if (cached && cached.size === file.sig.size
              && cached.mtimeMs === Number(BigInt(file.sig.mtimeNs) / 1_000_000n)) {
            hashes[index] = cached.hash;
            fresh.set(file.rel, { ...file.sig, hash: cached.hash });
            continue;
          }
          const hash = await new Promise<string>((resolve, reject) => {
            const hasher = createHash("sha256");
            const stream = fs.createReadStream(file.abs);
            stream.on("error", reject);
            stream.on("data", (chunk) => hasher.update(chunk));
            stream.on("end", () => resolve(hasher.digest("hex")));
          });
          cache?.set(file.abs, {
            size: file.sig.size,
            mtimeMs: Number(BigInt(file.sig.mtimeNs) / 1_000_000n),
            hash,
          });
          fresh.set(file.rel, { ...file.sig, hash });
          hashes[index] = hash;
        }
      }),
    );

    // Diff in the original (sorted) order so the tarball is reproducible and the
    // counters are deterministic regardless of which hash finished first.
    for (let index = 0; index < localFiles.length; index++) {
      const file = localFiles[index]!;
      if (remote.get(file.rel) === hashes[index]) { result.skipped += 1; continue; }
      changed.push({ rel: file.rel, abs: file.abs });
      result.sent += 1;
      result.bytesSent += file.sig.size;
    }

    const hashMs = clock() - tHash0;
    const tUpload0 = clock();

    // 4. Ship the changed files as ONE tarball and extract it in the guest. The
    //    extract's exit is checked, so a failure surfaces (never a silent partial);
    //    the manifest also fails safe — any omitted file is re-sent next call.
    if (changed.length > 0) await this.uploadAndExtractTarball(changed, localRoot, remoteRoot, fsp);
    // Only after the upload succeeded — persisting earlier would record files
    // as synced that never made it.
    if (!options.cache) await saveStatCache(cacheFile, fresh);
    const round = (v: number) => Math.round(v * 10) / 10;
    result.timings = {
      manifestMs: round(manifestMs),
      walkMs: round(walkMs),
      hashMs: round(hashMs),
      uploadMs: round(clock() - tUpload0),
    };
    return result;
  }

  /** Fetch the VM's file manifest under `path` -> Map(rel_path -> sha256), via the
   * host-first `op: "manifest"` op (no FC boot; works on a never-run VM). */
  private async remoteManifest(
    path: string,
  ): Promise<{ entries: Map<string, string>; truncated: boolean }> {
    const payload = await this._client._request<{
      entries?: Array<{ path?: unknown; hash?: unknown }>;
      truncated?: unknown;
    }>("POST", `${vmPath(this.id)}/sync`, { op: "manifest", path }, this.baseUrl);
    const out = new Map<string, string>();
    if (Array.isArray(payload.entries)) {
      for (const entry of payload.entries) {
        if (entry && typeof entry.path === "string" && typeof entry.hash === "string") {
          out.set(entry.path, entry.hash);
        }
      }
    }
    // The server caps the walk (SYNC_MANIFEST_MAX_ENTRIES, 50_000) and reports
    // `truncated`. Past the cap every omitted file looks absent, so the diff
    // marks it changed and re-uploads it: correct, but it silently turns the
    // delta sync into a full sync on exactly the trees where the delta matters
    // most. Surfacing it lets the caller see that rather than wonder why a
    // "delta" sync moves the whole tree every time.
    return { entries: out, truncated: payload.truncated === true };
  }

  /** One-round-trip directory upload: stream a gzip tar to `/sync-stream` and
   * let the server untar it IN THE GUEST before responding.
   *
   * Params ride in the query string, not headers: the auth middleware strips
   * `x-arker-*` from untrusted callers and would erase them.
   *
   * The body is sent raw (`application/octet-stream`) — no base64, so none of
   * the +33% inflation the JSON write path pays. */
  /**
   * Every streaming upload goes through this call site so authentication,
   * content type, retries, and error parsing stay consistent.
   *
   * `body` is a factory, not a value: a retried attempt needs a fresh body,
   * and a stream can only be consumed once.
   */
  private async syncStreamPost(
    query: Record<string, string>,
    body: () => BodyInit,
    what: string,
  ): Promise<void> {
    const url = `${this.baseUrl}${vmPath(this.id)}/sync-stream?${new URLSearchParams(query)}`;
    const attempts = this._client._retryAttempts();
    for (let attempt = 0; attempt < attempts; attempt++) {
      let res: Response;
      try {
        const payload = body();
        const init: RequestInit & { duplex?: "half" } = {
          method: "POST",
          headers: {
            ...this._client._authHeaders(),
            "content-type": "application/octet-stream",
          },
          body: payload,
        };
        // Only meaningful for a streamed body, and some runtimes REJECT the
        // request outright when it is set alongside a plain byte body — which
        // surfaces as an opaque "fetch failed", not an HTTP status.
        if (typeof (payload as ReadableStream | undefined)?.getReader === "function") {
          init.duplex = "half";
        }
        res = await this._client._fetch(url, init);
      } catch (error) {
        if (attempt === attempts - 1) {
          const message = error instanceof Error ? error.message : String(error);
          throw new ArkerError("unavailable", `${what} failed: ${message}`, 0);
        }
        await sleep(this._client._retryDelay(attempt));
        continue;
      }
      if (res.ok) return;
      const parsed = await parseErrorResponse(res, `${what} failed (${res.status})`);
      // 413 is the router's body cap, not a transient fault — never retry it.
      if (!RETRYABLE_HTTP.has(res.status) || attempt === attempts - 1) {
        throw new ArkerError(parsed.code, parsed.message, res.status);
      }
      await sleep(this._client._retryDelay(attempt));
    }
  }

  private async syncStreamExtract(
    tar: Uint8Array,
    remoteRoot: string,
    mode: "tar" | "tar.gz",
  ): Promise<void> {
    await this.syncStreamPost(
      { path: remoteRoot, size: String(tar.byteLength), extract: mode },
      () => tar as BodyInit,
      "sync-stream extract",
    );
  }

  /**
   * Upload a tarball straight from disk to keep memory flat for large trees.
   *
   * The body is a factory so a retry gets a FRESH read stream — a consumed
   * stream cannot be replayed, which is why `syncStreamPost` takes a factory
   * rather than a value.
   */
  private async syncStreamExtractFile(
    localTar: string,
    size: number,
    remoteRoot: string,
    mode: "tar" | "tar.gz",
  ): Promise<void> {
    const fs = await import("node:fs");
    const { Readable } = await import("node:stream");
    await this.syncStreamPost(
      { path: remoteRoot, size: String(size), extract: mode },
      () => Readable.toWeb(fs.createReadStream(localTar)) as unknown as BodyInit,
      "sync-stream extract",
    );
  }

  /**
   * Decide whether gzip earns its keep for this file set.
   *
   * The guest pays for decompression: gunzip measures ~3.4x a plain
   * untar (294ms vs 86ms for 2000 files), so gzipping an already-compressed
   * tree (images, video, archives, binaries) is a pure loss at both ends. Source
   * trees, by contrast, compress ~4:1 and are well worth it.
   *
   * Samples the head of a handful of files rather than compressing everything
   * twice. Falls back to compressing when the sample is too small to be
   * meaningful, preserving the previous always-gzip behaviour for tiny syncs.
   */
  private async shouldCompress(
    changed: Array<{ rel: string; abs: string }>,
    fsp: typeof import("node:fs").promises,
  ): Promise<boolean> {
    const zlib = await import("node:zlib");
    const { promisify } = await import("node:util");
    const gzipAsync = promisify(zlib.gzip);

    let raw = 0;
    let compressed = 0;
    for (const file of changed.slice(0, 8)) {
      try {
        const handle = await fsp.open(file.abs, "r");
        try {
          const buffer = Buffer.alloc(128 * 1024);
          const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
          if (bytesRead === 0) continue;
          const chunk = buffer.subarray(0, bytesRead);
          raw += chunk.length;
          compressed += (await gzipAsync(chunk)).length;
        } finally {
          await handle.close();
        }
      } catch {
        // Unreadable sample file — the tar step will surface it if it matters.
      }
    }
    if (raw < COMPRESSION_SAMPLE_MIN_BYTES) return true;
    return compressed / raw < COMPRESSION_WORTH_IT_RATIO;
  }

  /**
   * Write one file by streaming its bytes raw — no base64, no S3 hop.
   *
   * Measured in-region against the paths this replaces: 103-154 MB/s here vs
   * 41-50 MB/s inline and 25-56 MB/s presigned. Parity with the inline path is
   * verified: identical bytes, size and mode, nested parent directories are
   * created, and the optional `sha256` is enforced by the guest (a wrong digest
   * is rejected 409 rather than silently accepted).
   */
  private async syncWriteStream(path: string, data: Uint8Array, sha256?: string): Promise<void> {
    const query: Record<string, string> = { path, size: String(data.length) };
    if (sha256) query.sha256 = sha256;
    await this.syncStreamPost(query, () => data as BodyInit, "sync write");
  }

  /** Pack the changed files (paths relative to `localRoot`) into one gzip tar
   * and extract it in the guest — via `/sync-stream` where available, else by
   * uploading and running `tar -x`. Uses node-tar, which reads the files from
   * disk preserving mode (exec bits) + mtime. */
  private async uploadAndExtractTarball(
    changed: Array<{ rel: string; abs: string }>,
    localRoot: string,
    remoteRoot: string,
    fsp: typeof import("node:fs").promises,
  ): Promise<void> {
    const tar = await import("tar");
    const os = await import("node:os");
    const nodePath = await import("node:path");

    // gzip when it pays: source trees compress ~4:1 (a Linux checkout goes
    // 319 MB -> 70 MB) and this tarball is ONE request, so compressing is a
    // large win there. On an already-compressed tree it is a double loss —
    // wasted CPU here plus ~3.4x the extraction cost in the guest — so sample
    // first rather than always gzipping. `tar -xf` detects the compression
    // format, so either choice extracts correctly.
    const compress = await this.shouldCompress(changed, fsp);
    const mode = compress ? "tar.gz" : "tar";
    const localTar = nodePath.join(os.tmpdir(), `arker-sync-${ulid()}.${mode}`);
    try {
      await tar.create(
        { file: localTar, cwd: localRoot, gzip: compress },
        changed.map((entry) => entry.rel),
      );

      // One round trip. `/sync-stream?extract=tar.gz` streams the
      // tarball to the guest over vsock and untars it THERE before responding.
      // The fallback path is upload plus a separate `run("tar -xf")` — two
      // round-trips, with the extract going through the user run scheduler
      // where it can queue behind an active foreground run.
      //
      // A 404 selects the upload-and-extract path.
      try {
        if (this._client._supportsStreamingBody()) {
          const { size } = await fsp.stat(localTar);
          await this.syncStreamExtractFile(localTar, size, remoteRoot, mode);
        } else {
          // Caller-supplied fetch: may not accept a stream body, so buffer.
          await this.syncStreamExtract(await fsp.readFile(localTar), remoteRoot, mode);
        }
        return;
      } catch (error) {
        if (!(error instanceof ArkerError) || error.code !== "not_found") {
          throw error; // real failure (auth, path escape, size) must not be masked
        }
      }

      // A 404 selects the inline or presigned write path. Do not call `sync()`
      // here because it uses the same streaming route.
      const remoteTar = `/tmp/.arker-sync-${ulid()}.${mode}`;
      await this.syncWriteInline(remoteTar, await fsp.readFile(localTar));

      // `set -e` + explicit rm: any extract failure exits non-zero; the tarball
      // is removed on success. Missing parent dirs are created by mkdir/tar.
      const q = shellQuoteSingle;
      const cmd =
        `set -e; mkdir -p ${q(remoteRoot)}; ` +
        `tar -xf ${q(remoteTar)} -C ${q(remoteRoot)}; rm -f ${q(remoteTar)}`;
      const res = await this.run(cmd);
      if (res.state === "failed" || res.exitCode !== 0) {
        const stderr = (res.stderr ?? "").slice(0, 300);
        throw new ArkerError(
          "internal",
          `syncDir tar extract failed (exit=${res.exitCode}, state=${res.state}): ${stderr}`,
          200,
        );
      }
    } finally {
      await fsp.unlink(localTar).catch(() => {});
    }
  }

  private async syncRead(path: string): Promise<Uint8Array> {
    const request: SyncReadOperationRequest = { op: "read", path };
    const response = await this._client._request<SyncReadInlineResponse | SyncReadPresignedResponse>(
      "POST",
      `${vmPath(this.id)}/sync`,
      request,
      this.baseUrl,
    );
    if ("content" in response) return decodeBytes(response.content, response.encoding);
    const signed = await this._client._fetch(response.presigned_url);
    if (!signed.ok) throw new ArkerError("internal", `signed GET failed: ${signed.status}`, signed.status);
    return new Uint8Array(await signed.arrayBuffer());
  }

  private async syncWriteInline(path: string, data: Uint8Array): Promise<void> {
    const result = await this.sendOneWrite({
      path,
      size: data.length,
      upload_id: ulid(),
      content: bytesToBase64(data),
      start: 0,
      end: data.length,
      is_secret: false,
    });
    assertWriteComplete(result, "inline write");
  }

  private async sendOneWrite(entry: SyncWriteEntry): Promise<SyncWriteResult> {
    let lastError: SyncEntryError | undefined;
    const attempts = this._client._retryAttempts();
    for (let attempt = 0; attempt < attempts; attempt++) {
      const request: SyncWriteOperationRequest = {
        op: "write",
        writes: [entry],
      };
      const response = await this._client._request<SyncWriteResponse>("POST", `${vmPath(this.id)}/sync`, request, this.baseUrl);
      const result = response.results[0];
      if (!result) throw new ArkerError("internal", "write response missing results[0]", 200);
      const error = result.error ?? undefined;
      if (!error) return result;
      lastError = error;
      if (!isRetryable(200, error) || attempt === attempts - 1) break;
      await sleep(this._client._retryDelay(attempt));
    }
    throw new ArkerError(lastError?.code ?? "internal", lastError?.message ?? "write failed", 200);
  }

  /**
   * Update this VM's description, resource allocation, authorized SSH keys,
   * and/or network policy via `PATCH /v1/vms/{id}`. Returns the updated `Vm`.
   *
   * Accepts either a `PatchVmRequest` or flat resource fields
   * (`{ vcpu, memory_mib, disk_mib }`), which are folded into `resources`.
   * `policies` is a complete replacement for the VM's network policy, the
   * same document `setPolicies` accepts; an empty doc (`{}` or
   * `{ policies: [] }`) clears it to allow-all. Omit it to leave the current
   * policy unchanged.
   */
  async update(
    request:
      | PatchVmRequest
      | (ResourcesInput &
          Pick<PatchVmRequest, "description" | "ssh_public_keys" | "policies">),
  ): Promise<Vm> {
    const r = request as PatchVmRequest &
      ResourcesInput & { resources?: ResourcesInput | null };
    const body: PatchVmRequest =
      r.resources !== undefined || (r.vcpu === undefined && r.memory_mib === undefined && r.disk_mib === undefined)
        ? {
            description: r.description,
            resources: r.resources,
            ssh_public_keys: r.ssh_public_keys,
            policies: r.policies,
          }
        : {
            description: r.description,
            resources: {
              vcpu: r.vcpu ?? null,
              memory_mib: r.memory_mib ?? null,
              disk_mib: r.disk_mib ?? null,
            },
            ssh_public_keys: r.ssh_public_keys,
            policies: r.policies,
          };
    return this._client._request("PATCH", vmPath(this.id), body, this.baseUrl);
  }

  async delete(): Promise<DeleteVmResponse> {
    return this._client._request("DELETE", vmPath(this.id), undefined, this.baseUrl);
  }

  /**
   * Read this VM's network policy document.
   */
  async getPolicies(): Promise<PolicyDoc> {
    return this._client._request<PolicyDoc>("GET", `${vmPath(this.id)}/policies`, undefined, this.baseUrl);
  }

  /**
   * Replace this VM's network policy with `doc`. The response is the stored
   * policy document, including response-only hostname and warning fields.
   *
   *     await vm.setPolicies({
   *       policies: [
   *         { type: "outbound",
   *           match: { hosts: ["github.com"], ports: [443] },
   *           action: "allow" },
   *         { type: "outbound", action: "deny" },
   *       ],
   *     });
   */
  async setPolicies(doc: PolicyDoc): Promise<PolicyDoc> {
    return this._client._request<PolicyDoc>("PUT", `${vmPath(this.id)}/policies`, doc, this.baseUrl);
  }

  // ── Syncs: bindings of a filesystem into this VM at a path ────────
  async listSyncs(opts: ListSyncsOptions = {}): Promise<ListSyncsResponse> {
    const query: ListSyncsParameters = {
      cursor: opts.cursor, limit: opts.limit, filesystem_id: opts.filesystemId,
    };
    return this._client._request("GET", buildQuery(`${vmPath(this.id)}/syncs`, query), undefined, this.baseUrl);
  }

  async createSync(request: {
    filesystemId: SyncCreateRequest["filesystem_id"];
    path?: SyncCreateRequest["path"];
  }): Promise<Sync> {
    const body: SyncCreateRequest = {
      filesystem_id: request.filesystemId,
      path: request.path,
    };
    return this._client._request<Sync>("POST", `${vmPath(this.id)}/syncs`, body, this.baseUrl);
  }

  async deleteSync(syncId: string): Promise<DeleteSyncResponse> {
    return this._client._request("DELETE", `${vmPath(this.id)}/syncs/${pathSegment(syncId)}`, undefined, this.baseUrl);
  }

  // ── Runs ──────────────────────────────────────────────────────────
  async listRuns(opts: ListRunsOptions = {}): Promise<ListRunsResponse> {
    const query: ListRunsParameters = {
      cursor: opts.cursor, limit: opts.limit, state: opts.state,
      started_after: opts.startedAfter, started_before: opts.startedBefore, completed_after: opts.completedAfter,
    };
    return this._client._request("GET", buildQuery(`${vmPath(this.id)}/runs`, query), undefined, this.baseUrl);
  }

  /** Fetch a past run.
   *
   * `stdout`/`stderr` come back decoded, the same as {@link run}, alongside
   * `stdoutBytes`/`stderrBytes` for output that is not text. */
  async getRun(runId: string): Promise<RunRecord> {
    return decodeWireRun(
      await this._client._request<Run>(
        "GET",
        `${vmPath(this.id)}/runs/${pathSegment(runId)}`,
        undefined,
        this.baseUrl,
      ),
    );
  }

  async cancelRun(runId: string): Promise<CancelRunResponse> {
    return this._client._request("DELETE", `${vmPath(this.id)}/runs/${pathSegment(runId)}`, undefined, this.baseUrl);
  }

  // ── Sessions ──────────────────────────────────────────────────────
  async listSessions(opts: ListSessionsOptions = {}): Promise<ListSessionsResponse> {
    const query: ListSessionsParameters = {
      cursor: opts.cursor, limit: opts.limit, state: opts.state,
    };
    return this._client._request("GET", buildQuery(`${vmPath(this.id)}/sessions`, query), undefined, this.baseUrl);
  }

  /** Create a new session on this VM.
   *
   * `cwd` is optional. Omit it to start the session in the VM's own default
   * working directory (a fresh Ubuntu VM's default account home directory),
   * rather than a value this SDK picks. That account has passwordless sudo,
   * so package installs and other privileged commands don't need a
   * separate authentication step. */
  async createSession(request: CreateSessionRequest = {}): Promise<Session> {
    return this._client._request("POST", `${vmPath(this.id)}/sessions`, request, this.baseUrl);
  }

  async getSession(sessionId: string): Promise<Session> {
    return this._client._request("GET", `${vmPath(this.id)}/sessions/${pathSegment(sessionId)}`, undefined, this.baseUrl);
  }

  async deleteSession(sessionId: string): Promise<DeleteSessionResponse> {
    return this._client._request("DELETE", `${vmPath(this.id)}/sessions/${pathSegment(sessionId)}`, undefined, this.baseUrl);
  }

  /**
   * Update a session via `PATCH /v1/vms/{id}/sessions/{sid}`: resize its PTY
   * (`cols`/`rows`) and/or set the idle `timeoutSecs`. Works whether or not a
   * PTY is currently attached — the REST equivalent of {@link PtyConnection.resize}
   * (which sends an in-band control frame on the live WebSocket).
   */
  async updateSession(
    sessionId: string,
    update: {
      cols?: PatchSessionRequest["cols"];
      rows?: PatchSessionRequest["rows"];
      timeoutSecs?: PatchSessionRequest["timeout_secs"];
    },
  ): Promise<PatchSessionResponse> {
    const request: PatchSessionRequest = {
      cols: update.cols,
      rows: update.rows,
      timeout_secs: update.timeoutSecs,
    };
    return this._client._request<PatchSessionResponse>(
      "PATCH",
      `${vmPath(this.id)}/sessions/${pathSegment(sessionId)}`,
      request,
      this.baseUrl,
    );
  }

  async connectPty(options: PtyConnectOptions = {}): Promise<PtyConnection> {
    const sessionId = options.sessionId ?? sessionIdFrom(await this.createSession());
    const useTicket = options.useTicket ?? !isNodeRuntime();
    const params = {
      cols: options.cols,
      rows: options.rows,
      command: options.command,
      persist: options.persist,
      cancel_ttl_secs:
        options.cancelTtlSecs && options.cancelTtlSecs > 0
          ? Math.floor(options.cancelTtlSecs)
          : undefined,
    };
    let ticket: string | undefined;
    if (useTicket) {
      const response = await this._client._request<PtyTicketResponse>(
        "POST",
        `${vmPath(this.id)}/sessions/${pathSegment(sessionId)}/pty-ticket`,
        {},
        this.baseUrl,
      );
      ticket = response.ticket;
    }
    const url = buildPtyWebSocketUrl(this.baseUrl, this.id, sessionId, { ...params, ticket });
    const factory = options.webSocketFactory ?? (useTicket ? browserPtyWebSocketFactory : nodePtyWebSocketFactory);
    const socket = await factory(url, useTicket ? {} : { headers: this._client._authHeaders() });
    return new PtyConnectionImpl(sessionId, socket);
  }
}

// ── Helpers ────────────────────────────────────────────────────────

function buildQuery(path: string, params: Record<string, unknown>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    usp.append(key, String(value));
  }
  const qs = usp.toString();
  return qs ? `${path}?${qs}` : path;
}

function buildPtyWebSocketUrl(
  baseUrl: string,
  vmId: string,
  sessionId: string,
  params: { cols?: number; rows?: number; command?: string; persist?: boolean; cancel_ttl_secs?: number; ticket?: string },
): string {
  const url = new URL(`${normalizeBaseUrl(baseUrl)}${vmPath(vmId)}/sessions/${pathSegment(sessionId)}/pty`);
  if (url.protocol === "https:") url.protocol = "wss:";
  else if (url.protocol === "http:") url.protocol = "ws:";
  else throw new Error(`unsupported PTY WebSocket protocol: ${url.protocol}`);
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function sessionIdFrom(session: Session): string {
  return session.session_id;
}

function isNodeRuntime(): boolean {
  return typeof process !== "undefined" && Boolean(process.versions?.node);
}

async function nodePtyWebSocketFactory(url: string, init: PtyWebSocketFactoryInit): Promise<PtyWebSocketLike> {
  const ws = await import("ws");
  return new ws.default(url, { headers: init.headers }) as unknown as PtyWebSocketLike;
}

function browserPtyWebSocketFactory(url: string): PtyWebSocketLike {
  if (typeof globalThis.WebSocket !== "function") {
    throw new Error("WebSocket is not available in this runtime");
  }
  return new globalThis.WebSocket(url);
}

class PtyConnectionImpl implements PtyConnection {
  readonly ready: Promise<void>;
  private readonly dataListeners = new Set<(data: Uint8Array) => void>();
  private readonly closeListeners = new Set<(event: PtyCloseEvent) => void>();
  private readonly errorListeners = new Set<(error: unknown) => void>();

  constructor(readonly sessionId: string, private readonly socket: PtyWebSocketLike) {
    try {
      socket.binaryType = "arraybuffer";
    } catch {
      // Some WebSocket implementations expose binaryType as read-only.
    }
    this.ready = waitForSocketOpen(socket);
    addSocketListener(socket, "message", (event) => {
      const data = messageData(event);
      if (data !== undefined) this.emitData(bytesFromMessageData(data));
    });
    addSocketListener(socket, "close", (event) => this.emitClose(closeEvent(event)));
    addSocketListener(socket, "error", (event) => this.emitError(event));
  }

  onData(listener: (data: Uint8Array) => void): () => void {
    this.dataListeners.add(listener);
    return () => this.dataListeners.delete(listener);
  }

  onClose(listener: (event: PtyCloseEvent) => void): () => void {
    this.closeListeners.add(listener);
    return () => this.closeListeners.delete(listener);
  }

  onError(listener: (error: unknown) => void): () => void {
    this.errorListeners.add(listener);
    return () => this.errorListeners.delete(listener);
  }

  send(data: PtyInput): void {
    this.socket.send(ptyInputBytes(data));
  }

  resize(cols: number, rows: number): void {
    this.socket.send(JSON.stringify({ type: "resize", cols: clampPtyDimension(cols), rows: clampPtyDimension(rows) }));
  }

  kill(): void {
    this.socket.send(JSON.stringify({ type: "kill" }));
  }

  close(code?: number, reason?: string): void {
    this.socket.close(code, reason);
  }

  private emitData(data: Uint8Array): void {
    for (const listener of this.dataListeners) listener(data);
  }

  private emitClose(event: PtyCloseEvent): void {
    for (const listener of this.closeListeners) listener(event);
  }

  private emitError(error: unknown): void {
    for (const listener of this.errorListeners) listener(error);
  }
}

function waitForSocketOpen(socket: PtyWebSocketLike): Promise<void> {
  if (socket.readyState === 1) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let removeOpen: (() => void) | undefined;
    let removeError: (() => void) | undefined;
    let removeClose: (() => void) | undefined;
    const cleanup = () => {
      removeOpen?.();
      removeError?.();
      removeClose?.();
    };
    removeOpen = addSocketListener(socket, "open", () => {
      cleanup();
      resolve();
    });
    removeError = addSocketListener(socket, "error", (event) => {
      cleanup();
      reject(event instanceof Error ? event : new Error("PTY WebSocket failed to open"));
    });
    removeClose = addSocketListener(socket, "close", (event) => {
      cleanup();
      const ev = closeEvent(event);
      reject(new Error(`PTY WebSocket closed before opening${ev.code ? ` (${ev.code})` : ""}`));
    });
  });
}

function addSocketListener(
  socket: PtyWebSocketLike,
  type: string,
  listener: (event: unknown) => void,
): () => void {
  if (socket.addEventListener) {
    socket.addEventListener(type, listener);
    return () => socket.removeEventListener?.(type, listener);
  }
  if (socket.on) {
    const nodeListener = (...args: unknown[]) => {
      if (type === "message") listener({ data: args[0] });
      else if (type === "close") listener({ code: args[0], reason: args[1] });
      else listener(args[0]);
    };
    socket.on(type, nodeListener);
    return () => socket.off?.(type, nodeListener);
  }
  return () => {};
}

function messageData(event: unknown): unknown {
  if (event && typeof event === "object" && "data" in event) {
    return (event as { data?: unknown }).data;
  }
  return undefined;
}

function closeEvent(event: unknown): PtyCloseEvent {
  if (!event || typeof event !== "object") return {};
  const raw = event as { code?: unknown; reason?: unknown };
  return {
    code: typeof raw.code === "number" ? raw.code : undefined,
    reason: typeof raw.reason === "string" ? raw.reason : undefined,
  };
}

function bytesFromMessageData(data: unknown): Uint8Array {
  if (typeof data === "string") return new TextEncoder().encode(data);
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  if (Array.isArray(data)) {
    const chunks = data.map(bytesFromMessageData);
    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  }
  return new Uint8Array();
}

function ptyInputBytes(data: PtyInput): Uint8Array | ArrayBuffer {
  if (typeof data === "string") return new TextEncoder().encode(data);
  return data;
}

function clampPtyDimension(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(1000, Math.trunc(value)));
}

function normalizeBaseUrl(baseUrl: string): string {
  const trimmed = baseUrl.trim().replace(/\/+$/, "");
  if (!trimmed) throw new Error("baseUrl must not be empty");
  return trimmed;
}

function normalizePlacementLabel(name: "provider" | "region", value: string): string {
  const normalized = value.trim().toLowerCase();
  if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(normalized)) {
    throw new Error(`${name} must be a valid DNS label`);
  }
  return normalized;
}

function computeBaseUrl(provider: string, region: string): string {
  // Regional endpoints encode the provider and region in the hostname.
  const normalizedProvider = normalizePlacementLabel("provider", provider);
  const normalizedRegion = normalizePlacementLabel("region", region);
  const placement = `${normalizedProvider}-${normalizedRegion}`;
  if (placement.length > 63) {
    throw new Error("provider and region produce a DNS label longer than 63 characters");
  }
  return `https://${placement}.arker.ai/api`;
}

function optionalComputeProvider(value: unknown): ComputeProvider | null {
  if (typeof value !== "string") return null;
  try {
    return normalizePlacementLabel("provider", value);
  } catch {
    return null;
  }
}

function normalizeRetry(retry: RetryOptions | false | undefined): RetryConfig {
  if (retry === false) {
    return { attempts: 1, baseDelayMs: 0, maxDelayMs: 0, jitterMs: 0 };
  }
  return {
    attempts: Math.max(1, Math.floor(retry?.attempts ?? DEFAULT_RETRY_ATTEMPTS)),
    baseDelayMs: Math.max(0, retry?.baseDelayMs ?? DEFAULT_RETRY_BASE_DELAY_MS),
    maxDelayMs: Math.max(0, retry?.maxDelayMs ?? DEFAULT_RETRY_MAX_DELAY_MS),
    jitterMs: Math.max(0, retry?.jitterMs ?? DEFAULT_RETRY_JITTER_MS),
    hintCapMs: retry?.maxDelayMs !== undefined ? Math.max(0, retry.maxDelayMs) : undefined,
  };
}

function env(name: string): string | undefined {
  const value = (globalThis as unknown as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name];
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

function vmPath(vmId: string): string {
  return `/v1/vms/${pathSegment(vmId)}`;
}

function pathSegment(value: string): string {
  return encodeURIComponent(value);
}

function withoutUndefined(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withoutUndefined);
  if (!value || typeof value !== "object") return value;
  const output: JsonObject = {};
  for (const [key, entry] of Object.entries(value as JsonObject)) {
    if (entry !== undefined) output[key] = withoutUndefined(entry);
  }
  return output;
}

/** Terminal state for a finished run.
 *
 * A negative `exitCode` means no process status was obtained — the run was
 * killed or the compute was lost — which is `"failed"`. Keeps the synchronous
 * run result and `getRun()` reporting the same state for the same run. */
function terminalRunState(state: unknown, exitCode: number): string {
  if (exitCode < 0) return "failed";
  return typeof state === "string" ? state : "completed";
}

function parseRunResponse(payload: unknown): RunResult {
  const body = objectPayload(payload, "run response");
  if (typeof body.stdout === "string") {
    const stdout = stringValue(body.stdout, "run response.stdout");
    const stdoutEncoding = stringField(body.stdout_encoding, "run response.stdout_encoding");
    const stderr = stringValue(body.stderr, "run response.stderr");
    const stderrEncoding = stringField(body.stderr_encoding, "run response.stderr_encoding");
    const exitCode = numberField(body.exit_code, "run response.exit_code");
    return {
      type: "completed",
      runId: typeof body.run_id === "string" ? body.run_id : undefined,
      state: terminalRunState(body.state, exitCode),
      stdout: asText(decodeBytes(stdout, stdoutEncoding)),
      stderr: asText(decodeBytes(stderr, stderrEncoding)),
      stdoutBytes: decodeBytes(stdout, stdoutEncoding),
      stderrBytes: decodeBytes(stderr, stderrEncoding),
      exitCode,
      sessionId: typeof body.session_id === "string" ? body.session_id : null,
      failReason: typeof body.fail_reason === "string" ? body.fail_reason : null,
      memoryRequestedMib: optionalNumberOrNull(body.memory_requested_mib),
      memoryAchievedMib: optionalNumberOrNull(body.memory_achieved_mib),
      memoryPartial: typeof body.memory_partial === "boolean" ? body.memory_partial : undefined,
    };
  }
  if (typeof body.run_id === "string") {
    return {
      type: "background",
      runId: body.run_id,
      sessionId: typeof body.session_id === "string" ? body.session_id : null,
      state: typeof body.state === "string" ? body.state : "running",
    };
  }
  throw new ArkerError("internal", "unrecognized run response shape", 200);
}

/** Decode a wire run record into the public `Run`, converting `stdout`/`stderr`
 * from their encoded strings to bytes. The single boundary where the
 * run-status wire shape becomes a caller-facing one. */
/** Decode command output for the caller-facing `stdout`/`stderr`.
 *
 * Never throws — `TextDecoder` is non-fatal, so bytes that are not valid UTF-8
 * become U+FFFD. Defined once so the conversion cannot drift between result
 * types. */
function asText(data: Uint8Array): string {
  return new TextDecoder().decode(data);
}

function decodeWireRun(wire: Run): RunRecord {
  const stdoutBytes = decodeBytes(wire.stdout, wire.stdout_encoding);
  const stderrBytes = decodeBytes(wire.stderr, wire.stderr_encoding);
  return {
    ...wire,
    stdout: asText(stdoutBytes),
    stderr: asText(stderrBytes),
    stdoutBytes,
    stderrBytes,
  };
}

/** Project a terminal run-status (`Run`) into the `CompletedRunResult` shape
 * that a synchronous run() resolves to. The stored run carries no memory
 * override fields, so those stay undefined.
 *
 * `run.stdout`/`run.stderr` are already bytes — decoded at the wire boundary —
 * so they pass through untouched. */
function runToCompletedResult(run: RunRecord): CompletedRunResult {
  return {
    type: "completed",
    runId: run.run_id,
    state: run.state,
    stdout: run.stdout,
    stderr: run.stderr,
    stdoutBytes: run.stdoutBytes,
    stderrBytes: run.stderrBytes,
    exitCode: run.exit_code ?? (run.state === "completed" ? 0 : 1),
    failReason: run.fail_reason ?? null,
  };
}

async function requestJson<T>(
  method: HttpMethod,
  url: string,
  body: unknown,
  requestHeaders: Record<string, string>,
  fetchImpl: FetchLike,
  http2: boolean,
  retry: RetryConfig,
  maxQueueingSecs?: number,
): Promise<T> {
  const headers = { ...requestHeaders };
  let requestBody: string | undefined;
  if (body !== undefined) {
    headers["content-type"] = "application/json";
    requestBody = JSON.stringify(withoutUndefined(body));
  }

  // queueing_timeout swaps the retry budget from attempt count to wall-clock
  // window; retry: false (attempts = 1) still means exactly one request.
  const queueingDeadline =
    maxQueueingSecs !== undefined && maxQueueingSecs > 0 && retry.attempts > 1
      ? Date.now() + maxQueueingSecs * 1000
      : undefined;

  for (let attempt = 0; ; attempt++) {
    if (queueingDeadline !== undefined && attempt > 0 && isObject(body)) {
      // Retries re-send the remaining window.
      const remainingSecs = Math.max(
        1,
        Math.ceil((queueingDeadline - Date.now()) / 1000),
      );
      requestBody = JSON.stringify(
        withoutUndefined({ ...body, queueing_timeout: remainingSecs }),
      );
    }
    try {
      const { status, ok, text } = await sendRequest(
        url,
        { method, headers, body: requestBody },
        fetchImpl,
        http2,
      );
      const payload = parseJson(text);
      const parsedError = extractError(payload);

      if (isRetryable(status, parsedError)) {
        const delay = retryDelay(retry, attempt, parsedError);
        if (canRetryAgain(retry, attempt, queueingDeadline, delay)) {
          await sleep(delay);
          continue;
        }
      }

      if (parsedError) {
        throw new ArkerError(parsedError.code, parsedError.message, status);
      }
      if (!ok) {
        throw new ArkerError(
          "internal",
          text.slice(0, 300) || `HTTP ${status}`,
          status,
        );
      }

      return payload as T;
    } catch (error) {
      if (error instanceof ArkerError) throw error;
      const delay = retryDelay(retry, attempt);
      if (canRetryAgain(retry, attempt, queueingDeadline, delay)) {
        await sleep(delay);
        continue;
      }
      const message = error instanceof Error ? error.message : String(error);
      throw new ArkerError("unavailable", message, 0);
    }
  }
}

/** No window: the attempt count is the budget. With one: the retry's sleep
 * must still land inside the window. */
function canRetryAgain(
  retry: RetryConfig,
  attempt: number,
  queueingDeadline: number | undefined,
  delayMs: number,
): boolean {
  if (queueingDeadline !== undefined) return Date.now() + delayMs < queueingDeadline;
  return attempt < retry.attempts - 1;
}

function parseJson(text: string): unknown {
  if (!text) return {};
  try { return JSON.parse(text) as unknown; } catch { return undefined; }
}

function extractError(payload: unknown): ParsedError | undefined {
  if (!isObject(payload)) return undefined;
  if (Object.keys(payload).length === 1 && isObject(payload.error)) {
    const error: Partial<ErrorResponse["error"]> = payload.error;
    if (
      typeof error.code === "string" &&
      typeof error.message === "string" &&
      typeof error.timestamp === "string"
    ) {
      return {
        code: error.code,
        message: error.message,
        retryAfterS: wireRetryAfter(error.retry_after),
      };
    }
  }
  return undefined;
}

/** Seconds the server asked us to wait, or undefined if it did not say usefully. */
function wireRetryAfter(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return undefined;
  return value;
}

function isRetryable(status: number, error?: ParsedError): boolean {
  if (RETRYABLE_HTTP.has(status)) return true;
  if (!error) return false;
  if (RETRYABLE_CODES.has(error.code as ErrorCode)) return true;
  if (error.code !== "internal") return false;
  return TRANSIENT_HINTS.some((hint) => error.message.includes(hint));
}

function retryDelay(retry: RetryConfig, attempt: number, error?: ParsedError): number {
  // The server's hint beats backoff, bounded by an explicitly configured
  // maxDelayMs — the caller's latency budget outranks the server. The DEFAULT
  // max only shapes backoff; applying it here would neuter real capacity waits.
  const hint = error?.retryAfterS;
  if (hint !== undefined) {
    return Math.min(hint * 1000, retry.hintCapMs ?? Number.POSITIVE_INFINITY) + jitter(retry.jitterMs);
  }
  const base = Math.min(retry.maxDelayMs, retry.baseDelayMs * 2 ** attempt);
  return base + jitter(retry.jitterMs);
}

function jitter(maxMs: number): number { return Math.floor(Math.random() * (maxMs + 1)); }

async function sleep(ms: number): Promise<void> { await new Promise((resolve) => setTimeout(resolve, ms)); }

function objectPayload(value: unknown, context: string): JsonObject {
  if (!isObject(value)) throw new ArkerError("internal", `${context} must be an object`, 200);
  return value;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringField(value: unknown, context: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ArkerError("internal", `${context} must be a non-empty string`, 200);
  }
  return value;
}

function stringValue(value: unknown, context: string): string {
  if (typeof value !== "string") throw new ArkerError("internal", `${context} must be a string`, 200);
  return value;
}

function numberField(value: unknown, context: string): number {
  if (typeof value !== "number") throw new ArkerError("internal", `${context} must be a number`, 200);
  return value;
}

function optionalNumberOrNull(value: unknown): number | null | undefined {
  if (value === null || typeof value === "number") return value;
  return undefined;
}

function assertWriteComplete(result: SyncWriteResult, context: string): void {
  if (result.complete && result.written) return;
  throw new ArkerError("internal", `${context} did not complete`, 200);
}

function ulid(): string {
  const crypto = globalThis.crypto;
  if (!crypto?.getRandomValues) throw new Error("crypto.getRandomValues is required in this runtime");
  const time = BigInt(Date.now()) & ((1n << 48n) - 1n);
  const rand = new Uint8Array(10);
  crypto.getRandomValues(rand);
  let raw = (time << 80n) | rand.reduce((acc, byte) => (acc << 8n) | BigInt(byte), 0n);
  const out: string[] = [];
  for (let i = 0; i < 26; i++) {
    out.push(ULID_ALPHABET[Number(raw & 31n)]!);
    raw >>= 5n;
  }
  return out.reverse().join("");
}

function decodeBytes(text: string, encoding: string): Uint8Array {
  if (encoding === "base64") return base64ToBytes(text);
  return new TextEncoder().encode(text);
}

function bytesToBase64(data: Uint8Array): string {
  const buffer = bufferConstructor();
  if (buffer) return buffer.from(data).toString("base64");
  let binary = "";
  for (let offset = 0; offset < data.length; offset += 0x8000) {
    const chunk = data.subarray(offset, offset + 0x8000);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

/** Result of {@link VM.syncDir}. */
export interface SyncDirResult {
  /** Files uploaded (new or changed on the VM). */
  sent: number;
  /** Files already up-to-date on the VM (skipped). */
  skipped: number;
  /** Total uncompressed bytes of the uploaded files. */
  bytesSent: number;
  /**
   * True when the VM's manifest hit the server's entry cap (50,000 files) and
   * was truncated. Everything beyond the cap is invisible to the diff, so it is
   * treated as changed and re-uploaded — the sync stays CORRECT but stops being
   * a delta. If you see this, split the sync into subdirectories.
   */
  manifestTruncated?: boolean;
  /** Wall-clock ms per phase. Useful when a sync is slower than expected:
   * `hash` dominating means the caller is not reusing a `cache`, since an
   * uncached call re-reads and re-hashes every file in the tree. */
  timings?: { manifestMs: number; walkMs: number; hashMs: number; uploadMs: number };
}

/** Options for {@link VM.syncDir}. */
export interface SyncDirOptions {
  /** Caller-owned accelerator cache: absolute local path -> {size, mtimeMs, hash}.
   * Reused across calls it skips re-hashing files whose (size, mtime) are
   * unchanged. Pure optimization — it never affects which files are sent. */
  cache?: Map<string, { size: number; mtimeMs: number; hash: string }>;

  /** Skip the remote manifest round-trip and send everything.
   *
   * The manifest exists to avoid re-sending unchanged files. On a FIRST sync
   * into a fresh directory it is guaranteed empty, so the round-trip costs
   * ~184ms to learn nothing. Set this when you know the destination is new
   * (e.g. straight after a fork).
   *
   * Safe by construction: an empty manifest means "send everything", which is
   * what a first sync does anyway. Setting it wrongly re-sends files that were
   * already there — wasteful, never incorrect. */
  assumeEmpty?: boolean;
}

/** POSIX-single-quote a string so it is safe inside a `/bin/sh` command. */
function shellQuoteSingle(value: string): string {
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

function base64ToBytes(text: string): Uint8Array {
  const buffer = bufferConstructor();
  if (buffer) {
    const decoded = buffer.from(text, "base64");
    return new Uint8Array(decoded.buffer, decoded.byteOffset, decoded.byteLength);
  }
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

function bufferConstructor(): BufferConstructorLike | undefined {
  return (globalThis as unknown as { Buffer?: BufferConstructorLike }).Buffer;
}

// ── Transport ───────────────────────────────────────────────────────

interface TransportResponse {
  status: number;
  ok: boolean;
  text: string;
}

type Http2Module = typeof import("node:http2");
type Http2Session = ReturnType<Http2Module["connect"]>;

let http2Module: Promise<Http2Module | null> | undefined;
function loadHttp2(): Promise<Http2Module | null> {
  return (http2Module ??= (async () => {
    const proc = (globalThis as unknown as { process?: { versions?: { node?: string } } }).process;
    if (!proc?.versions?.node) return null;
    try {
      return (await import(/* webpackIgnore: true */ /* @vite-ignore */ "node:http2")) as Http2Module;
    } catch {
      return null;
    }
  })());
}

// Must exceed the server's 120s sync window, or the request is torn down just
// as the background ack arrives and run() never gets to poll.
const HTTP2_REQUEST_TIMEOUT_MS = 300_000;
const BUN_RUNTIME = Boolean((globalThis as unknown as {
  process?: { versions?: { bun?: string } };
}).process?.versions?.bun);

// One HTTP/2 session per origin; concurrent requests multiplex over it as streams.
// `confirmed` flips on the first response so the caller can fall back to fetch if the
// origin turns out not to speak HTTP/2.
class Http2Connection {
  confirmed = false;
  private streams = 0;
  private readonly session: Http2Session;

  constructor(http2: Http2Module, origin: string) {
    this.session = http2.connect(origin);
    this.session.on("error", () => {});
  }

  get closed(): boolean {
    return this.session.closed || this.session.destroyed;
  }

  request(method: string, path: string, headers: Record<string, string>, body?: string): Promise<TransportResponse> {
    // Ref the socket only while requests are in flight, so a pending request keeps
    // the process alive but an idle connection still lets it exit.
    if (this.streams === 0) this.session.ref();
    this.streams++;
    return new Promise<TransportResponse>((resolve, reject) => {
      const stream = this.session.request({ ...headers, ":method": method, ":path": path });
      let status = 0;
      let text = "";
      stream.setEncoding("utf8");
      let timeout: ReturnType<typeof setTimeout> | undefined;
      if (BUN_RUNTIME) {
        // Bun attaches stream timeouts to the shared session socket.
        timeout = setTimeout(
          () => stream.destroy(new Error("HTTP/2 request timed out")),
          HTTP2_REQUEST_TIMEOUT_MS,
        );
        timeout.unref();
      } else {
        stream.setTimeout(HTTP2_REQUEST_TIMEOUT_MS, () => stream.destroy(new Error("HTTP/2 request timed out")));
      }
      stream.on("response", (responseHeaders) => {
        timeout?.refresh();
        this.confirmed = true;
        status = Number(responseHeaders[":status"]) || 0;
      });
      stream.on("data", (chunk: string) => {
        timeout?.refresh();
        text += chunk;
      });
      stream.on("end", () => {
        if (timeout) clearTimeout(timeout);
        resolve({ status, ok: status >= 200 && status < 300, text });
      });
      stream.on("error", (error) => {
        if (timeout) clearTimeout(timeout);
        reject(error);
      });
      stream.end(body);
    }).finally(() => {
      if (--this.streams === 0) this.session.unref();
    });
  }
}

const http2Connections = new Map<string, Http2Connection | null>();

async function sendRequest(
  url: string,
  init: { method: string; headers: Record<string, string>; body?: string },
  fetchImpl: FetchLike,
  http2Enabled: boolean,
): Promise<TransportResponse> {
  if (http2Enabled) {
    const http2 = await loadHttp2();
    if (http2) {
      const { origin, pathname, search } = new URL(url);
      const cached = http2Connections.get(origin); // null => origin known not to speak HTTP/2
      if (cached !== null) {
        let connection = cached;
        if (!connection || connection.closed) {
          connection = new Http2Connection(http2, origin);
          http2Connections.set(origin, connection);
        }
        try {
          return await connection.request(init.method, `${pathname}${search}`, init.headers, init.body);
        } catch (error) {
          // Origin proved non-HTTP/2 before any success: disable it, use fetch.
          if (connection.confirmed) throw error;
          http2Connections.set(origin, null);
        }
      }
    }
  }
  const response = await fetchImpl(url, init as RequestInit);
  return { status: response.status, ok: response.ok, text: await response.text() };
}
