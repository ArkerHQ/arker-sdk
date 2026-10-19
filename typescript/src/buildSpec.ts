/**
 * Parse a Dockerfile into the instruction list the build driver walks.
 *
 * Text in, instructions out. No network, no VM, no filesystem. `build.ts`
 * executes them: `FROM` becomes a real image fork, `RUN` becomes `vm.run`,
 * `COPY` becomes a sync into the VM.
 *
 * ## Why this is in the SDK
 *
 * `COPY` sources are local, so Dockerfile builds are driven by the SDK.
 * `COPY` uses `vm.syncDir` and sends only changed files.
 *
 * ## Scope
 *
 * Supported: `FROM`, `RUN`, `COPY`, `ADD <url>`, `ENV`, `WORKDIR`, `USER`,
 * `ARG`, `LABEL`, `EXPOSE`, `ENTRYPOINT`, `CMD`, `SHELL`.
 *
 * `SHELL` replaces the interpreter for the shell form of later `RUN`,
 * `ENTRYPOINT` and `CMD`, as it does in Docker. It is state, not a step:
 * nothing executes it. Exec form is left alone throughout, because it does not
 * go through an interpreter at all.
 *
 * Refused by name rather than silently dropped: multi-stage builds (more than
 * one `FROM`, or a `COPY --from=`), an `ARG`-substituted `FROM` (it resolves
 * through a real image fork, so it must be literal), a local-source `ADD` (its
 * one advantage over `COPY` is archive auto-extraction, which we do not do),
 * and every other directive.
 *
 * `${VAR}` substitution into instruction text is not performed. A `RUN` sees
 * `$VAR` because the shell expands it; `WORKDIR /app-$VERSION` does not.
 */

import { Copy as AstCopy, DockerfileParser, From, Instruction } from "dockerfile-ast";

/** A Dockerfile this SDK will not build, with the reason named. */
export class DockerfileError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DockerfileError";
  }
}

/** `RUN <cmd>` - executed against the VM via `vm.run`. */
export type RunStep = { kind: "run"; command: string };
/**
 * `COPY <src>... <dest>` - synced from the build context into the VM.
 * `sources` are as written; the driver resolves and globs them locally.
 */
export type CopyStep = { kind: "copy"; sources: string[]; dest: string; chown?: string };
/** `ADD <url> <dest>` - fetched inside the guest. */
export type AddStep = { kind: "add"; url: string; dest: string; checksum?: string };
/** `ENV k=v` - exported for later RUNs, and persists onto the delivered VM. */
export type EnvStep = { kind: "env"; pairs: [string, string][] };
/** `WORKDIR <dir>` - created if missing, then entered. */
export type WorkdirStep = { kind: "workdir"; path: string };
/** `USER <name>` - subsequent RUNs execute as this user. */
export type UserStep = { kind: "user"; name: string };
/** `ARG name[=default]`; without a default it is inert, as in Docker. */
export type ArgStep = { kind: "arg"; name: string; default?: string };
/** Recorded, but with no runtime effect on a VM. */
export type InertStep = { kind: "label" | "expose" | "entrypoint" | "cmd"; value: string };

export type Step =
  | RunStep
  | CopyStep
  | AddStep
  | EnvStep
  | WorkdirStep
  | UserStep
  | ArgStep
  | InertStep;

export type ParsedDockerfile = { baseImage: string; steps: Step[] };

const KNOWN = new Set([
  "FROM", "RUN", "COPY", "ADD", "ENV", "WORKDIR",
  "USER", "ARG", "LABEL", "EXPOSE", "ENTRYPOINT", "CMD", "SHELL",
]);

/**
 * Quote one argument for a shell command line, the way `shlex.quote` does:
 * only when it needs it, so ordinary tokens stay readable.
 */
function shellQuote(value: string): string {
  if (value.length > 0 && /^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

/** The argv of an exec-form instruction, or undefined if it is shell form. */
function execForm(raw: string): string[] | undefined {
  const trimmed = raw.trim();
  if (!trimmed.startsWith("[")) return undefined;
  try {
    const argv = JSON.parse(trimmed);
    if (Array.isArray(argv) && argv.every((item) => typeof item === "string")) return argv;
  } catch {
    // Not valid JSON, so it was never exec form: a shell-form command may
    // legitimately start with `[` (the `test` builtin), so this is not an error.
  }
  return undefined;
}

/**
 * Render RUN/ENTRYPOINT/CMD as one command line.
 *
 * Exec form does not go through a shell in Docker, but the only execution
 * primitive here is "a command line in a shell", so each element is quoted and
 * joined. That invokes the same argv a direct exec would, via the shell's own
 * fork+exec.
 *
 * `shell` is the interpreter set by a preceding `SHELL`. Docker applies it to
 * the SHELL FORM of all three instructions, and to exec form of none of them,
 * which is what the branch below encodes. Absent a `SHELL` the argument is
 * passed through untouched, so the guest's own default shell runs it as before.
 */
function commandLine(raw: string, shell: string[] | undefined): string {
  const argv = execForm(raw);
  if (argv !== undefined) return argv.map(shellQuote).join(" ");
  if (shell === undefined) return raw.trim();
  return [...shell, raw.trim()].map(shellQuote).join(" ");
}

function keyValuePairs(instruction: Instruction, directive: string): [string, string][] {
  const properties = (instruction as unknown as {
    getProperties?: () => { getName(): string; getValue(): string | null }[];
  }).getProperties?.();
  if (!properties || properties.length === 0) {
    throw new DockerfileError(`${directive} requires at least one key`);
  }
  return properties.map((property) => [property.getName(), property.getValue() ?? ""]);
}

/** Parse and validate `text`, or throw {@link DockerfileError}. */
export function parseDockerfile(text: string): ParsedDockerfile {
  const instructions = DockerfileParser.parse(text).getInstructions();
  let baseImage: string | undefined;
  let fromCount = 0;
  const steps: Step[] = [];
  let shell: string[] | undefined;

  for (const instruction of instructions) {
    const directive = (instruction.getKeyword() ?? "").toUpperCase();
    const argument = (instruction.getArgumentsContent() ?? "").trim();

    if (!KNOWN.has(directive)) {
      throw new DockerfileError(
        `\`${directive}\` is not supported; supported directives are ` +
          `${[...KNOWN].sort().join(", ")} (single-stage only)`,
      );
    }

    switch (directive) {
      case "FROM": {
        fromCount += 1;
        if (fromCount > 1) {
          throw new DockerfileError(
            "multi-stage dockerfiles are not supported (found more than one FROM): " +
              "flatten to a single stage, or fork from an already-built image reference",
          );
        }
        const image = instruction instanceof From
          ? instruction.getImage() ?? ""
          : argument.split(/\s+AS\s+/i)[0];
        if (image.includes("$")) {
          throw new DockerfileError(
            `FROM referencing a build ARG (\`${image}\`) is not supported: give a ` +
              "literal base image reference, for example `FROM ubuntu:24.04`",
          );
        }
        if (!image) throw new DockerfileError("FROM requires an image reference");
        baseImage = image;
        break;
      }
      case "RUN": {
        if (!argument) throw new DockerfileError("RUN requires a command");
        steps.push({ kind: "run", command: commandLine(argument, shell) });
        break;
      }
      case "COPY": {
        const copy = instruction as AstCopy;
        const flags = copy.getFlags?.() ?? [];
        const from = flags.find((flag) => flag.getName() === "from");
        if (from) {
          throw new DockerfileError(
            `COPY --from=${from.getValue()} is not supported: cross-stage copies need ` +
              "multi-stage builds, which this path does not implement",
          );
        }
        const chown = flags.find((flag) => flag.getName() === "chown")?.getValue();
        const tokens = copy.getArguments().map((a) => a.getValue());
        if (tokens.length < 2) {
          throw new DockerfileError(
            `COPY requires at least one source and a destination, got: ${argument}`,
          );
        }
        steps.push({
          kind: "copy",
          sources: tokens.slice(0, -1),
          dest: tokens[tokens.length - 1]!,
          ...(chown ? { chown } : {}),
        });
        break;
      }
      case "ADD": {
        const addFlags = (instruction as AstCopy).getFlags?.() ?? [];
        const checksum = addFlags.find((flag) => flag.getName() === "checksum")?.getValue();
        const tokens = instruction.getArguments().map((a) => a.getValue());
        if (tokens.length !== 2) {
          throw new DockerfileError(
            `ADD ${argument} is not supported: only \`ADD <url> <dest>\` is, with a ` +
              "single URL source and destination",
          );
        }
        const [source, dest] = tokens as [string, string];
        if (!/^https?:\/\//.test(source)) {
          throw new DockerfileError(
            `ADD ${source} is not supported: only a URL source is. Use COPY for local ` +
              "files. ADD's archive auto-extraction is not implemented, which is why a " +
              "local ADD is refused rather than treated as COPY",
          );
        }
        steps.push({ kind: "add", url: source, dest, ...(checksum ? { checksum } : {}) });
        break;
      }
      case "ENV": {
        steps.push({ kind: "env", pairs: keyValuePairs(instruction, "ENV") });
        break;
      }
      case "ARG": {
        const properties = (instruction as unknown as {
          getProperties(): { getName(): string; getValue(): string | null }[];
        }).getProperties();
        if (properties.length === 0) throw new DockerfileError("ARG requires a name");
        for (const property of properties) {
          const value = property.getValue();
          steps.push({
            kind: "arg",
            name: property.getName(),
            ...(value === null ? {} : { default: value }),
          });
        }
        break;
      }
      case "WORKDIR": {
        if (!argument) throw new DockerfileError("WORKDIR requires a directory");
        steps.push({ kind: "workdir", path: argument });
        break;
      }
      case "USER": {
        if (!argument) throw new DockerfileError("USER requires a username");
        steps.push({ kind: "user", name: argument });
        break;
      }
      case "EXPOSE": {
        if (!argument) throw new DockerfileError("EXPOSE requires a port");
        steps.push({ kind: "expose", value: argument });
        break;
      }
      case "LABEL": {
        steps.push({ kind: "label", value: argument });
        break;
      }
      case "ENTRYPOINT": {
        steps.push({ kind: "entrypoint", value: commandLine(argument, shell) });
        break;
      }
      case "CMD": {
        steps.push({ kind: "cmd", value: commandLine(argument, shell) });
        break;
      }
      case "SHELL": {
        const argv = execForm(argument);
        if (argv === undefined) {
          throw new DockerfileError(
            'SHELL must be given in exec form, for example `SHELL ["/bin/bash", "-c"]`',
          );
        }
        if (argv.length === 0) {
          throw new DockerfileError("SHELL requires at least one element, the interpreter to run");
        }
        shell = argv;
        break;
      }
    }
  }

  if (!baseImage) throw new DockerfileError("dockerfile has no FROM instruction");
  return { baseImage, steps };
}
