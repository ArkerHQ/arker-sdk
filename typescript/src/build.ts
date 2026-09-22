/**
 * Drive a parsed Dockerfile against a VM.
 *
 * `FROM` is not executed here: the caller forks from it through the ordinary
 * server-side image path, which owns the registry pull, the OCI to ext4
 * conversion and the template cache. Everything after it is walked by
 * {@link applySteps} against the resulting VM.
 *
 * ## Shell state
 *
 * `ENV`, `WORKDIR` and `ARG`-with-a-default run real `export`/`cd` commands
 * rather than prefixing every later command. A VM session keeps its exported
 * environment and working directory between runs, so one `export` sticks for
 * each subsequent `RUN` and for the caller's own `run()` calls on the VM they
 * get back. Prefixing would have lost that state the moment the build ended.
 *
 * `USER` is the exception: there is no per-session "become this user", so each
 * later `RUN` is wrapped in `su -p`. `USER` therefore affects the build but
 * does not carry onto the delivered VM, which is a real divergence from Docker
 * and is documented rather than hidden.
 *
 * ## COPY
 *
 * Sources resolve against the build context root and transfer through the VM's
 * own file APIs: a directory via `syncDir` (manifest-diffed, so a rebuild sends
 * only what changed), a single file via `sync`. No shell is involved, so there
 * is nothing to quote and the paths never reach a command line.
 *
 * `COPY` runs unwrapped, outside any `USER` shell: Docker writes copied files
 * as root unless `--chown` says otherwise, and the transfer APIs write as the
 * guest agent rather than a session user. `--chown` is applied afterwards as an
 * explicit `chown -R`.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import nodePath from "node:path";

import type { Step } from "./buildSpec.js";
import { loadDockerignore } from "./dockerignore.js";

/** A Dockerfile that parsed but cannot be built, with the reason named. */
export class BuildError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BuildError";
  }
}

/** The slice of `VM` this module uses. */
export type BuildOptions = {
  queueingTimeout?: number;
};

export type BuildTarget = {
  run(command: string, options?: Record<string, unknown>): Promise<unknown>;
  sync(path: string, data: Uint8Array | string): Promise<void>;
  syncDir(localDir: string, remoteDir: string, options?: Record<string, unknown>): Promise<unknown>;
};

/**
 * Quote an `ENV`/`ARG` value for `export`.
 *
 * A value containing `$` is DOUBLE-quoted so the guest shell expands it.
 * `ENV PATH=/opt/bin:$PATH` is one of the most common lines in any Dockerfile,
 * and single-quoting it sets the literal string `$PATH` — breaking every later
 * `RUN` and every `run()` the customer makes, with a "command not found" three
 * steps away from the cause.
 *
 * The trade-off is deliberate: double quotes make the value an expansion
 * context, so `$(...)` would also run. That is guest-side, where `RUN` already
 * grants arbitrary execution, so it costs nothing already spent. Backslash,
 * backtick and `"` are escaped so the value cannot break out of the quoting.
 */
function envValue(value: string): string {
  if (!value.includes("$")) return shellQuote(value);
  const escaped = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/`/g, "\\`");
  return `"${escaped}"`;
}

function shellQuote(value: string): string {
  if (value.length > 0 && /^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * Resolve one COPY source against the context, expanding globs.
 *
 * Globs expand HERE, against the local filesystem, never by a shell. That
 * removes the whole class of quoting and metacharacter problems a shell-side
 * expansion carries, and an unmatched glob becomes a clear error rather than a
 * literal path that fails later.
 */
function resolveSources(source: string, contextRoot: string): string[] {
  const root = fs.realpathSync(contextRoot);
  const full = nodePath.resolve(root, source);

  let matches: string[];
  if (/[*?]/.test(source)) {
    const dir = nodePath.dirname(full);
    const pattern = nodePath.basename(full);
    const regex = new RegExp(
      `^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`,
    );
    const entries = fs.existsSync(dir) ? fs.readdirSync(dir) : [];
    // A leading `.` must be matched EXPLICITLY, as in the shell and in
    // Python's glob. Without this `COPY * /app` shipped `.env` and `.git` in
    // TypeScript but not in Python: the same Dockerfile, two different images.
    const wantsDotfiles = pattern.startsWith(".");
    matches = entries
      .filter((entry) => (wantsDotfiles || !entry.startsWith(".")) && regex.test(entry))
      .sort()
      .map((entry) => nodePath.join(dir, entry));
  } else {
    matches = [full];
  }

  if (matches.length === 0) {
    throw new BuildError(`COPY ${source}: no such file or directory in the build context`);
  }

  const outsideContext = (candidate: string): boolean =>
    candidate !== root && !candidate.startsWith(root + nodePath.sep);

  return matches.map((match) => {
    // The context root is the boundary, exactly as `docker build` treats it.
    // Checked LEXICALLY first, before existence: `COPY ../secrets` should be
    // reported as an escape whether or not the path happens to exist, and
    // realpathSync would throw on a missing one before we could say so.
    if (outsideContext(match)) {
      throw new BuildError(
        `COPY ${source} names a path outside the build context; everything COPY ` +
          `reads must live under ${contextRoot}`,
      );
    }
    if (!fs.existsSync(match)) {
      throw new BuildError(`COPY ${source}: no such file or directory in the build context`);
    }
    // Then again after resolving, which is what catches a symlink pointing out
    // of the tree rather than only a literal `..`.
    const real = fs.realpathSync(match);
    if (outsideContext(real)) {
      throw new BuildError(
        `COPY ${source} resolves outside the build context (via a symlink); everything ` +
          `COPY reads must live under ${contextRoot}`,
      );
    }
    return real;
  });
}

/**
 * Where one resolved source lands in the guest. Docker's rule: a destination
 * ending in `/`, or one receiving several sources, is a directory and each
 * source keeps its basename.
 */
function destinationFor(dest: string, sourcePath: string, multiple: boolean): string {
  if (dest.endsWith("/") || multiple) {
    return `${dest.replace(/\/+$/, "")}/${nodePath.basename(sourcePath)}`;
  }
  return dest;
}

/**
 * Run `command` and abort the build unless it succeeded.
 *
 * Docker fails a build on a non-zero `RUN`, and the reason is not tidiness: a
 * build that continues past a failure applies every later instruction on top of
 * it and hands back a VM that looks built and is not. `RUN npm install` failing
 * must not produce a "successful" image with no node_modules.
 *
 * A missing exit code is also a failure. It means a prompt ended the command
 * rather than its completion marker — an interpreter left running by an earlier
 * instruction swallowed it — so the command never ran at all.
 */
async function runChecked(
  vm: BuildTarget,
  command: string,
  what: string,
  options: BuildOptions = {},
): Promise<void> {
  const result = (await vm.run(
    command,
    options.queueingTimeout === undefined ? undefined : { queueing_timeout: options.queueingTimeout },
  )) as {
    exit_code?: number | null;
    exitCode?: number | null;
    stdout?: string;
    stderr?: string;
  };
  // `??` would treat an explicit null as absent and coalesce it to 0, which
  // laundered "the command never ran" into success and made the branch below
  // unreachable. Read the field, then decide.
  const raw = result?.exitCode !== undefined ? result.exitCode : result?.exit_code;
  const code = raw === undefined ? 0 : raw;
  if (code === 0) return;

  const detail = (result?.stderr || result?.stdout || "").trim();
  const suffix = detail ? `: ${detail.slice(0, 400)}` : "";
  if (code === null || code === undefined) {
    throw new BuildError(
      `${what} never reached the shell (an interpreter started by an earlier ` +
        `instruction is holding the session)${suffix}`,
    );
  }
  throw new BuildError(`${what} failed with exit code ${code}${suffix}`);
}

/** Execute every step after `FROM` against `vm`, in file order. */
export async function applySteps(
  vm: BuildTarget,
  steps: Step[],
  contextRoot: string,
  options: BuildOptions = {},
): Promise<void> {
  let currentUser: string | undefined;

  for (const step of steps) {
    let command: string | undefined;

    switch (step.kind) {
      case "run":
        command = step.command;
        break;
      case "env":
        command = `export ${step.pairs.map(([k, v]) => `${k}=${envValue(v)}`).join(" ")}`;
        break;
      case "arg":
        // No --build-arg channel exists, so an ARG with no default can only
        // ever be unset, the same as Docker leaves it.
        if (step.default === undefined) continue;
        command = `export ${step.name}=${envValue(step.default)}`;
        break;
      case "workdir": {
        // Docker creates a WORKDIR that does not exist; a bare `cd` would fail
        // on the first build against a fresh image.
        const quoted = shellQuote(step.path);
        command = `mkdir -p ${quoted} && cd ${quoted}`;
        break;
      }
      case "user":
        currentUser = step.name;
        continue;
      case "add": {
        // Fetched here, then written like a COPY: the guest needs no network
        // tooling and no shell for this at all.
        let payload: Uint8Array;
        try {
          const response = await fetch(step.url, { signal: AbortSignal.timeout(30_000) });
          if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
          payload = new Uint8Array(await response.arrayBuffer());
        } catch (error) {
          throw new BuildError(`ADD ${step.url}: ${String(error)}`);
        }
        verifyChecksum(step, payload);
        await vm.sync(step.dest, payload);
        continue;
      }
      case "copy":
        // Unwrapped by design; see the module docstring.
        await applyCopy(vm, step, contextRoot, options);
        continue;
      case "label":
      case "expose":
      case "entrypoint":
      case "cmd":
        // Recorded by the parser, no effect on a VM: nothing invokes a VM the
        // way `docker run` invokes an entrypoint, and there is no
        // `docker inspect` surface for LABEL/EXPOSE to configure.
        continue;
    }

    if (command === undefined) continue;
    // Only a RUN needs to become the USER. Wrapping `export`/`cd` would run
    // them in a `su -c` subshell that exits immediately, throwing the state
    // away — so `ENV` after `USER` silently did nothing at all. Docker treats
    // ENV/WORKDIR as metadata that USER does not affect.
    if (currentUser !== undefined && step.kind === "run") {
      command = `su -p ${shellQuote(currentUser)} -c ${shellQuote(command)}`;
    }
    await runChecked(vm, command, `${step.kind.toUpperCase()} step`, options);
  }
}

function verifyChecksum(step: Extract<Step, { kind: "add" }>, payload: Uint8Array): void {
  if (!step.checksum) return;
  const separator = step.checksum.indexOf(":");
  const algorithm = separator === -1 ? "" : step.checksum.slice(0, separator);
  const expected = separator === -1 ? "" : step.checksum.slice(separator + 1).toLowerCase();
  if (!expected) {
    throw new BuildError(
      `ADD ${step.url}: --checksum must be <algorithm>:<hex>, got ${JSON.stringify(step.checksum)}`,
    );
  }
  let digest: string;
  try {
    digest = crypto.createHash(algorithm).update(payload).digest("hex");
  } catch {
    throw new BuildError(`ADD ${step.url}: unknown checksum algorithm ${JSON.stringify(algorithm)}`);
  }
  if (digest !== expected) {
    throw new BuildError(
      `ADD ${step.url}: checksum mismatch, expected ${algorithm}:${expected} ` +
        `but the downloaded bytes are ${algorithm}:${digest}`,
    );
  }
}

async function applyCopy(
  vm: BuildTarget,
  step: Extract<Step, { kind: "copy" }>,
  contextRoot: string,
  options: BuildOptions = {},
): Promise<void> {
  // realpath BOTH sides. `resolveSources` returns resolved paths, and on a
  // platform where the context sits under a symlink (macOS /tmp ->
  // /private/tmp) comparing a resolved path against an unresolved root yields
  // a relative path like `../../private/tmp/...`, which matches no pattern and
  // silently disables every rule.
  const root = fs.realpathSync(contextRoot);
  const ignore = loadDockerignore(root);
  const relTo = (p: string) =>
    nodePath.relative(root, fs.realpathSync(p)).split(nodePath.sep).join("/");

  const resolved = step.sources
    .flatMap((source) => resolveSources(source, contextRoot))
    // A named source that is itself ignored is dropped, the same as Docker
    // dropping it from the context before COPY ever looks.
    .filter((p) => !ignore.ignores(relTo(p)));
  const multiple = resolved.length > 1;

  for (const path of resolved) {
    if (fs.statSync(path).isDirectory()) {
      // A directory source copies its CONTENTS into the destination, which is
      // what syncDir does: `COPY src /app/src` puts src's files at /app/src,
      // not at /app/src/src.
      const target = step.dest.replace(/\/+$/, "") || "/";
      // `.dockerignore` patterns are context-relative but syncDir reports
      // paths relative to the directory being synced, so re-anchor them.
      const prefix = relTo(path) === "" ? "" : `${relTo(path)}/`;
      await vm.syncDir(path, target, {
        ignore: (rel: string) => ignore.ignores(prefix + rel),
      });
    } else {
      const target = destinationFor(step.dest, path, multiple);
      await vm.sync(target, fs.readFileSync(path));
      if (fs.statSync(path).mode & 0o111) {
        await runChecked(
          vm,
          `chmod +x ${shellQuote(target)}`,
          `COPY ${nodePath.basename(path)}`,
          options,
        );
      }
    }
  }

  if (step.chown) {
    await runChecked(
      vm,
      `chown -R ${shellQuote(step.chown)} ${shellQuote(step.dest)}`,
      `COPY --chown=${step.chown}`,
      options,
    );
  }
}
