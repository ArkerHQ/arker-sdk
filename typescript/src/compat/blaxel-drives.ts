import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { SandboxDriveSetupError, type SandboxInstance } from "@blaxel/core";
import type { Arker, Filesystem, Mount, VM } from "../index.js";

type Filesystems = Pick<
  Arker,
  "createFilesystem" | "getFilesystem" | "listFilesystems" | "deleteFilesystem"
> &
  Pick<VM, "provider" | "region">;
type MountedVm = Pick<
  VM,
  "provider" | "region" | "createMount" | "listMounts" | "deleteMount"
>;
type Request = {
  mountPath: string;
  drivePath: string;
  driveName?: string;
  create?: { name?: string };
};
type Entry = {
  request: Request;
  name: string;
  generated: boolean;
  file?: Filesystem;
  created?: boolean;
  uncertain?: boolean;
  attempted?: boolean;
};
type WaitOptions = { timeoutMs?: number; signal?: AbortSignal };
type Binding = {
  driveName: string;
  mountPath: string;
  drivePath: string;
  readOnly: false;
};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new TypeError("Drive options must be an object");
  return value as Record<string, unknown>;
}
function only(value: Record<string, unknown>, keys: string[]): void {
  for (const key of Object.keys(value))
    if (!keys.includes(key) && value[key] !== undefined) {
      throw new TypeError(`Unsupported Arker drive option: ${key}`);
    }
}
function path(value: unknown): string {
  if (
    typeof value !== "string" ||
    !value.startsWith("/") ||
    value.includes("\0") ||
    value.includes("\\")
  ) {
    throw new TypeError("Drive paths must be absolute paths");
  }
  const parts = value.split("/").filter(Boolean);
  if (parts.some((part) => part === "." || part === ".."))
    throw new TypeError("Drive paths cannot contain traversal");
  return "/" + parts.join("/");
}
function request(value: unknown, getOrCreate = false): Request {
  const entry = object(value);
  only(entry, ["mountPath", "drivePath", "readOnly", "driveName", "create"]);
  if (entry.readOnly !== undefined && entry.readOnly !== false)
    throw new TypeError("Arker drive mounts support only readOnly=false");
  if (!entry.driveName === !entry.create)
    throw new TypeError(
      "Each mountDrives entry needs exactly one of 'driveName' or 'create'.",
    );
  let create: { name?: string } | undefined;
  if (entry.create) {
    const options = object(entry.create);
    only(options, ["name"]);
    if (options.name !== undefined && typeof options.name !== "string")
      throw new TypeError("Drive name must be a string");
    if (getOrCreate && !options.name)
      throw new TypeError(
        "With createIfNotExist, a new drive in mountDrives needs a name; otherwise every call would create another drive.",
      );
    create = { name: options.name as string | undefined };
  } else if (typeof entry.driveName !== "string")
    throw new TypeError("Drive name must be a string");
  if (path(entry.mountPath) === "/")
    throw new TypeError("Drive mounts cannot replace the root filesystem");
  return {
    mountPath: path(entry.mountPath),
    drivePath: path(entry.drivePath ?? "/"),
    ...(create ? { create } : { driveName: entry.driveName as string }),
  };
}
function status(error: unknown): number | undefined {
  if (
    error &&
    typeof error === "object" &&
    "status" in error &&
    typeof error.status === "number"
  )
    return error.status;
  return undefined;
}
function ambiguous(error: unknown): boolean {
  const code = status(error);
  return code === undefined || code < 400 || code >= 500 || code === 408;
}

async function named(
  filesystems: Filesystems,
  name: string,
): Promise<Filesystem> {
  let cursor: string | undefined;
  let found: Filesystem | undefined;
  const seen = new Set<string>();
  do {
    const page = await filesystems.listFilesystems({
      namePrefix: name,
      cursor,
    });
    for (const file of page.filesystems)
      if (file.name === name) {
        if (found && found.filesystem_id !== file.filesystem_id)
          throw new Error("Drive name is ambiguous");
        found = file;
      }
    cursor = page.next_cursor ?? undefined;
    if (cursor && seen.has(cursor))
      throw new Error("Filesystem cursor repeated");
    if (cursor) seen.add(cursor);
  } while (cursor);
  if (!found) throw new Error(`Drive ${name} was not found`);
  const file = await filesystems.getFilesystem(found.filesystem_id);
  if (file.name !== name)
    throw new Error("Drive identity changed during lookup");
  return file;
}
async function mounts(vm: MountedVm): Promise<Mount[]> {
  let cursor: string | undefined;
  const result: Mount[] = [],
    seen = new Set<string>();
  do {
    const page = await vm.listMounts({ cursor });
    result.push(...page.mounts);
    cursor = page.next_cursor ?? undefined;
    if (cursor && seen.has(cursor)) throw new Error("Mount cursor repeated");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return result;
}
/** Use the original authenticated transport and its explicit endpoint override. */
export function scopedFilesystems(vm: VM): Filesystems {
  const client = vm._client;
  const base = client._baseUrlFor(vm.id, {
    provider: vm.provider,
    region: vm.region,
  });
  const query = (path: string, values: Record<string, unknown>) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values))
      if (value !== undefined && value !== null) params.set(key, String(value));
    return params.size ? `${path}?${params}` : path;
  };
  return {
    provider: vm.provider,
    region: vm.region,
    listFilesystems: (opts = {}) =>
      client._request(
        "GET",
        query("/v1/filesystems", {
          cursor: opts.cursor,
          limit: opts.limit,
          name_prefix: opts.namePrefix,
        }),
        undefined,
        base,
      ),
    getFilesystem: (id) =>
      client._request(
        "GET",
        `/v1/filesystems/${encodeURIComponent(id)}`,
        undefined,
        base,
      ),
    createFilesystem: (body) =>
      client._request(
        "POST",
        "/v1/filesystems",
        body,
        base,
        undefined,
        undefined,
        false,
      ),
    deleteFilesystem: (id, opts = {}) =>
      client._request(
        "DELETE",
        query(`/v1/filesystems/${encodeURIComponent(id)}`, {
          only_if_unmounted: opts.onlyIfUnmounted,
        }),
        undefined,
        base,
        undefined,
        undefined,
        false,
      ),
  };
}
function placement(vm: MountedVm, filesystems: Filesystems): void {
  if (
    !vm.region ||
    !vm.provider ||
    vm.region !== filesystems.region ||
    vm.provider !== filesystems.provider
  ) {
    throw new Error(
      "Drive operations require a filesystem client bound to the ready VM's placement",
    );
  }
}
function same(row: Mount, file: Filesystem, wanted: Request): boolean {
  return (
    row.filesystem_id === file.filesystem_id &&
    path(row.path) === wanted.mountPath &&
    path(row.subpath ? "/" + row.subpath : "/") === wanted.drivePath
  );
}
function binding(file: Filesystem, row: Mount): Binding {
  return {
    driveName: file.name,
    mountPath: row.path,
    drivePath: row.subpath ? "/" + row.subpath : "/",
    readOnly: false,
  };
}

/** Public writable bindings; the client must use the ready VM's authenticated regional endpoint. */
export function blaxelDrives(
  vm: MountedVm,
  filesystems: Filesystems,
  options: WaitOptions = {},
) {
  placement(vm, filesystems);
  const timeout = options.timeoutMs ?? 30_000;
  if (!Number.isFinite(timeout) || timeout <= 0)
    throw new TypeError("Mount readiness timeout must be positive");
  const check = () => options.signal?.throwIfAborted();
  async function attach(
    wanted: Request,
    file: Filesystem,
    attempted: () => void = () => {},
  ) {
    check();
    if (file.region !== vm.region)
      throw new Error(`Drive ${file.name} is not in the sandbox region`);
    const atPath = (rows: Mount[]) => {
      const found = rows.filter((row) => path(row.path) === wanted.mountPath);
      if (found.length > 1 || (found[0] && !same(found[0], file, wanted)))
        throw new Error(`${wanted.mountPath} is not mounted as requested`);
      return found[0];
    };
    let row = atPath(await mounts(vm));
    const deadline = performance.now() + timeout;
    if (!row) {
      check();
      attempted(); // A lost response can still have installed the mount.
      try {
        row = await vm.createMount({
          filesystemId: file.filesystem_id,
          path: wanted.mountPath,
          ...(wanted.drivePath !== "/"
            ? { subpath: wanted.drivePath.slice(1) }
            : {}),
        });
        if (!same(row, file, wanted))
          throw new Error("Mount response does not match the request");
      } catch (error) {
        if (status(error) !== 409 && !ambiguous(error)) throw error;
        try {
          row = atPath(await mounts(vm));
        } catch {
          throw error;
        }
        if (!row) throw error; // Never replay an uncertain POST.
      }
    }
    for (;;) {
      check();
      row = atPath(await mounts(vm));
      if (!row) throw new Error("Mount disappeared before readiness");
      if (row.state === "mounted")
        return { ...binding(file, row), success: true };
      if (row.state === "failed")
        throw new Error(row.state_detail ?? "Drive mount failed");
      if (row.state !== "attaching")
        throw new Error("Mount has no authoritative readiness state");
      const remaining = deadline - performance.now();
      if (remaining <= 0)
        throw new Error(
          "Drive mount readiness timed out; the mount may continue",
        );
      await delay(Math.min(100, remaining), undefined, {
        signal: options.signal,
      });
    }
  }
  return {
    attach,
    async mount(value: unknown) {
      const wanted = request(value);
      if (wanted.create)
        throw new TypeError("drives.mount requires an existing driveName");
      return attach(wanted, await named(filesystems, wanted.driveName!));
    },
    async list(): Promise<Binding[]> {
      const result: Binding[] = [];
      for (const row of await mounts(vm))
        if (row.state === "mounted") {
          const file = await filesystems.getFilesystem(row.filesystem_id);
          if (file.region !== vm.region)
            throw new Error("Mounted drive has inconsistent placement");
          result.push(binding(file, row));
        }
      return result;
    },
    async unmount(mountPath: string) {
      check();
      const target = path(mountPath);
      const found = (await mounts(vm)).filter(
        (row) => path(row.path) === target,
      );
      if (found.length !== 1)
        throw new Error("Mount path does not identify one mount");
      const result = await vm.deleteMount(found[0]!.mount_id);
      if (!result.deleted) throw new Error("Mount deletion was not confirmed");
      return { mountPath: target, success: true };
    },
  };
}

/** Call before sandbox creation. No drive is created until mount receives a ready VM. */
export function prepareBlaxelDrives(
  values: unknown,
  options: { createIfNotExist?: boolean; region?: string } = {},
) {
  if (!Array.isArray(values))
    throw new TypeError("mountDrives must be an array");
  if (values.length && options.region !== undefined)
    throw new TypeError(
      "Explicit vendor region is unsupported for Arker drive setup",
    );
  const requests = values.map((value) =>
    request(value, options.createIfNotExist),
  );
  return {
    async mount(
      sandbox: SandboxInstance,
      vm: MountedVm,
      filesystems: Filesystems,
      wait: WaitOptions = {},
    ) {
      const entries: Entry[] = requests.map((request) => ({
        request,
        name:
          request.driveName ??
          (request.create?.name ||
            "drive-" + randomUUID().replaceAll("-", "").slice(0, 16)),
        generated: Boolean(request.create && !request.create.name),
      }));
      try {
        const drives = blaxelDrives(vm, filesystems, wait);
        // Serial preparation keeps ownership and rollback bounded; no POST is replayed.
        for (const entry of entries) {
          wait.signal?.throwIfAborted();
          if (!entry.request.create)
            entry.file = await named(filesystems, entry.name);
          else {
            try {
              entry.file = await filesystems.createFilesystem({
                name: entry.name,
              });
              entry.created = true;
            } catch (error) {
              if (status(error) === 409 && !entry.generated)
                entry.file = await named(filesystems, entry.name);
              else if (ambiguous(error)) {
                entry.uncertain = true;
                try {
                  entry.file = await named(filesystems, entry.name);
                } catch {
                  throw error;
                }
                // A matching name cannot prove ownership after a lost response.
                throw error;
              } else throw error;
            }
          }
          if (entry.file.name !== entry.name)
            throw new Error("Created drive identity does not match");
          await drives.attach(entry.request, entry.file, () => {
            entry.attempted = true;
          });
        }
        return drives;
      } catch (cause) {
        const cleanupErrors: unknown[] = [];
        let mounted: Set<string> | undefined;
        try {
          mounted = new Set((await mounts(vm)).map((row) => row.filesystem_id));
        } catch {
          /* Keep uncertain mounts. */
        }
        for (const entry of entries)
          if (
            entry.generated &&
            entry.created &&
            entry.file &&
            !mounted?.has(entry.file.filesystem_id) &&
            (!entry.attempted || mounted !== undefined)
          ) {
            try {
              const removed = await filesystems.deleteFilesystem(
                entry.file.filesystem_id,
                { onlyIfUnmounted: true },
              );
              if (removed.deleted) entry.file = undefined;
            } catch (error) {
              cleanupErrors.push(error);
            }
          }
        const error = new SandboxDriveSetupError(
          sandbox,
          entries.filter((entry) => entry.file).map((entry) => entry.name),
          entries
            .filter((entry) => (entry.created && entry.file) || entry.uncertain)
            .map((entry) => entry.name),
          cause,
        );
        if (cleanupErrors.length)
          Object.defineProperty(error, "cleanupErrors", {
            value: cleanupErrors,
          });
        throw error;
      }
    },
  };
}
