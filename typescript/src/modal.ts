import { createRequire } from "node:module";
import { InvalidError, ModalClient as NativeModalClient, type ModalClientParams, type Sandbox as NativeSandbox, type SandboxExecParams, type ContainerProcess, type SandboxTerminateParams } from "modal";
import type { RunCommandOptions } from "computesdk";

import {
  assertSupportedObjectKeys,
  guardUnsupported,
  isStringRecord,
  nativeInstance,
  type UniversalSandbox,
  shellQuote,
} from "./compat/common.js";
import { ModalAppRecipe, ModalVolumeRecipe, registryImage, registry, registryCredentials, localSecret, secretEnvironment, recipeService, creationDefaults } from "./compat/modal-recipes.js";
import { MODAL_DEFAULTS_MARKER, prepareExec, discardExec, writeDefaults } from "./compat/modal-defaults.js";
import { createNativeModalProvider } from "./compat/modal-provider.js";
import { rethrowModalExecError } from "./compat/modal-errors.js";
import type { VM } from "./index.js";
import { modalCommandHandle } from "./compat/modal-command.js";
import { modalFilesystem } from "./compat/modal-filesystem.js";
import { GuestProcesses } from "./compat/guest-process.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";

export interface ModalCompatConfig extends ModalClientParams {
  /** Modal token ID used only for native Modal fallback. */
  tokenId?: string;
  /** Modal token secret used only for native Modal fallback. */
  tokenSecret?: string;
  /** Arker provider options. Falls back to ARKER_* environment variables. */
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

type ModalExecOptions = SandboxExecParams;

const MODAL_CONFIG_KEYS = ["tokenId", "tokenSecret", "oauthRefreshToken", "oauthClientId", "oauthClientSecret", "oauthJwtKey", "environment", "endpoint", "timeoutMs", "maxRetries", "maxThrottleWaitSecs", "logger", "logLevel", "grpcMiddleware", "cpClient", "arker", "routerConfigPath"] as const;
const MODAL_EXEC_OPTION_KEYS = ["workdir", "env", "timeoutMs", "stdout", "stderr", "mode", "pty", "secrets"] as const;
const MODAL_CREATE_OPTION_KEYS = ["cpu", "cpuLimit", "memoryMiB", "memoryLimitMiB", "runtime", "gpu", "timeoutMs", "idleTimeoutMs", "workdir", "command", "env", "secrets", "volumes", "cloudBucketMounts", "pty", "encryptedPorts", "h2Ports", "unencryptedPorts", "blockNetwork", "outboundCidrAllowlist", "outboundDomainAllowlist", "experimentalOutboundPolicy", "inboundCidrAllowlist", "i6pn", "cloud", "regions", "verbose", "proxy", "readinessProbe", "name", "tags", "experimentalOptions", "customDomain", "includeOidcIdentityToken", "experimentalEnableSnapshot"] as const;

function assertOptionValue(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

function toRunCommandOptions(opts: unknown): RunCommandOptions | undefined {
  assertSupportedObjectKeys(opts, MODAL_EXEC_OPTION_KEYS, "modal", "Sandbox.exec");
  if (opts === undefined) return undefined;
  const modalOpts = opts as ModalExecOptions;

  assertOptionValue(
    modalOpts.pty === undefined || modalOpts.pty === false,
    "Sandbox.exec PTY is not supported by Arker; pty must be false or omitted.",
  );
  assertOptionValue(
    modalOpts.secrets === undefined || (Array.isArray(modalOpts.secrets) && modalOpts.secrets.length === 0),
    "Sandbox.exec secrets are not supported by Arker; secrets must be empty or omitted.",
  );

  assertOptionValue(
    modalOpts.workdir === undefined || typeof modalOpts.workdir === "string",
    "Sandbox.exec option 'workdir' must be a string in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.timeoutMs === undefined || typeof modalOpts.timeoutMs === "number",
    "Sandbox.exec option 'timeoutMs' must be a number in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.env === undefined || isStringRecord(modalOpts.env),
    "Sandbox.exec option 'env' must be an object in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.stdout === undefined || modalOpts.stdout === "pipe" || modalOpts.stdout === "ignore",
    "Sandbox.exec supports stdout: 'pipe' or 'ignore' in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.stderr === undefined || modalOpts.stderr === "pipe" || modalOpts.stderr === "ignore",
    "Sandbox.exec supports stderr: 'pipe' or 'ignore' in the Arker compatibility layer for modal.",
  );
  assertOptionValue(
    modalOpts.mode === undefined || modalOpts.mode === "text" || modalOpts.mode === "binary",
    "Sandbox.exec supports mode: 'text' or 'binary' in the Arker compatibility layer for modal.",
  );

  if (modalOpts.timeoutMs !== undefined && modalOpts.timeoutMs <= 0)
    throw new Error(`timeoutMs must be positive, got ${modalOpts.timeoutMs}`);
  if (modalOpts.timeoutMs && modalOpts.timeoutMs % 1000 !== 0)
    throw new Error(`timeoutMs must be a multiple of 1000ms, got ${modalOpts.timeoutMs}`);
  if (modalOpts.workdir !== undefined && !modalOpts.workdir.startsWith("/"))
    throw new InvalidError(`workdir must be an absolute path, got: ${modalOpts.workdir}`);
  for (const key of Object.keys(modalOpts.env ?? {})) {
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(key))
      throw new InvalidError(`Secret key name ${JSON.stringify(key)} is invalid for environment variables. Only letters, numbers, and underscores are allowed, and the name may not start with a number.`);
  }

  const runOptions: RunCommandOptions = {};
  if (modalOpts.workdir !== undefined) runOptions.cwd = modalOpts.workdir;
  if (modalOpts.env !== undefined) runOptions.env = modalOpts.env;
  if (modalOpts.timeoutMs !== undefined) runOptions.timeout = modalOpts.timeoutMs;

  return Object.keys(runOptions).length > 0 ? runOptions : undefined;
}

export class Sandbox {
  readonly sandboxId: string;
  readonly provider: string;
  #box: UniversalSandbox;
  #native: NativeSandbox | undefined;
  #processes: GuestProcesses | undefined;
  #filesystem: NativeSandbox["filesystem"] | undefined;

  constructor(box: UniversalSandbox) {
    this.#box = box;
    this.sandboxId = box.sandboxId;
    this.provider = box.provider;
    this.#native = nativeInstance(box) as NativeSandbox | undefined;
    this.#processes = box.provider === "arker" ? new GuestProcesses(box) : undefined;
  }

  get filesystem(): NativeSandbox["filesystem"] {
    if (this.#native) return this.#native.filesystem;
    return this.#filesystem ??= modalFilesystem((command, options) => this.exec(command, options as ModalExecOptions & { mode?: "text" }));
  }

  async exec(cmd: string[] | string, opts: ModalExecOptions & { mode: "binary" }): Promise<ContainerProcess<Uint8Array>>;
  async exec(cmd: string[] | string, opts?: ModalExecOptions & { mode?: "text" }): Promise<ContainerProcess<string>>;
  async exec(cmd: string[] | string, opts?: ModalExecOptions): Promise<ContainerProcess<string | Uint8Array>> {
    const argv = typeof cmd === "string" ? ["sh", "-c", cmd] : cmd;
    if (typeof this.#native?.exec === "function") {

      return opts?.mode === "binary"
        ? this.#native.exec(argv, opts as SandboxExecParams & { mode: "binary" })
        : this.#native.exec(argv, opts as SandboxExecParams & { mode?: "text" });
    }
    try {
      const environment = secretEnvironment(opts?.secrets);
      const runOptions = toRunCommandOptions({ ...opts, secrets: [] }) ?? {};
      runOptions.env = { ...environment, ...runOptions.env };
      if (!this.#processes) throw new Error("Native Modal sandbox instance is unavailable");
      if (!argv.length || argv.some(arg => typeof arg !== "string")) throw new TypeError("Modal exec requires a nonempty array of string arguments");
      const control = await prepareExec(this.#box, argv, runOptions.env, runOptions.cwd);
      let current;
      try {
        current = await this.#processes.exec({
          command: argv.map(shellQuote).join(" "), argv: control.argv, _modalControl: control.name, spawnFailureExitCode: 128, preserveDescendants: true, workingDir: control.name ? "/" : runOptions.cwd ?? "/", env: {},
          stdin: true, ...(runOptions.timeout !== undefined ? { timeout: runOptions.timeout } : {}),
        });
      } catch (error) {
        await discardExec(this.#box, control.name, error);
        throw error;
      }
      return opts?.mode === "binary"
        ? modalCommandHandle(this.#processes, current, { ...opts, secrets: [], mode: "binary" })
        : modalCommandHandle(this.#processes, current, { ...opts, secrets: [], mode: "text" });
    } catch (error) {
      return rethrowModalExecError(error, () => (this.#box as UniversalSandbox & { getInstance(): VM }).getInstance().refresh());
    }
  }

  async terminate(): Promise<void>;
  async terminate(params: { wait: true }): Promise<number>;
  async terminate(params?: SandboxTerminateParams): Promise<void | number> {
    if (this.#native) return params?.wait ? this.#native.terminate({ wait: true }) : this.#native.terminate();
    assertSupportedObjectKeys(params, ["wait"], "modal", "Sandbox.terminate");
    if (params?.wait) throw new Error("Waiting for a Modal entrypoint exit status is not supported on Arker");
    await this.#box.destroy();
  }
}

export class ModalClient {
  #sdk: CompatSdk;
  #native: NativeModalClient | undefined;
  #nativeConfig: ModalClientParams;
  #hasArker: boolean;

  #nativeClient(): NativeModalClient {
    return this.#native ??= new NativeModalClient(this.#nativeConfig);
  }

  readonly apps = recipeService(() => this.#nativeClient().apps, {
    fromName: async (name: string, options?: Parameters<NativeModalClient["apps"]["fromName"]>[1]) =>
      this.#hasArker ? new ModalAppRecipe(name, options) : this.#nativeClient().apps.fromName(name, options),
  });
  readonly images = recipeService(() => this.#nativeClient().images, {
    fromRegistry: (tag: string, secret?: Parameters<NativeModalClient["images"]["fromRegistry"]>[1]) => registryImage(this.#nativeClient(), tag, secret),
  });
  readonly secrets = recipeService(() => this.#nativeClient().secrets, {
    fromObject: (entries: Record<string, string>, options?: Parameters<NativeModalClient["secrets"]["fromObject"]>[1]) =>
      localSecret(this.#nativeClient(), entries, options),
  });
  readonly volumes = recipeService(() => this.#nativeClient().volumes, {
    fromName: async (name: string, options?: Parameters<NativeModalClient["volumes"]["fromName"]>[1]) =>
      this.#hasArker ? new ModalVolumeRecipe(name, options) : this.#nativeClient().volumes.fromName(name, options),
  });
  get cloudBucketMounts(): NativeModalClient["cloudBucketMounts"] { return this.#nativeClient().cloudBucketMounts; }
  get sandboxSnapshots(): NativeModalClient["sandboxSnapshots"] { return this.#nativeClient().sandboxSnapshots; }
  get proxies(): NativeModalClient["proxies"] { return this.#nativeClient().proxies; }
  get environments(): NativeModalClient["environments"] { return this.#nativeClient().environments; }

  close(): void { this.#native?.close(); }

  constructor(config?: ModalCompatConfig) {
    assertSupportedObjectKeys(config, MODAL_CONFIG_KEYS, "modal", "new ModalClient");
    const { arker, routerConfigPath, ...native } = config ?? {};
    this.#nativeConfig = native;
    this.#hasArker = !!(arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
    this.#sdk = createCompatSdk({
      arker,
      fallbackProvider: createNativeModalProvider(() => this.#nativeClient()),
      routerConfigPath,
      arkerLifetimePolicyId: "computesdk-modal-lifetime",
      mapArkerCreateOptions: ({ modalCreateParams, modalApp, modalImage, ...rest }) => {
        if (modalApp !== undefined || modalImage !== undefined) {
          if (!(modalApp instanceof ModalAppRecipe)) {
            throw new Error("Arker requires local app and image recipes; opaque native handles are unsupported");
          }
          rest = { ...rest, image: registry(modalImage, modalApp), registryAuth: registryCredentials(modalImage), metadata: { "computesdk-modal-app": modalApp.group() } };
        }
        if (arker?.policies && "policies" in arker.policies) {
          throw new Error("Modal lifetime requires named policies, not ordered policies");
        }
        const { timeoutMs, env, workdir, secrets, ...options } = (modalCreateParams ?? {}) as Record<string, unknown>;
        const defaults = creationDefaults({ env, workdir, secrets });
        if (Object.keys(defaults.env).length || workdir !== undefined) {
          rest = { ...rest, metadata: { ...(rest.metadata as Record<string, unknown> ?? {}), [MODAL_DEFAULTS_MARKER]: true } };
        }
        if (timeoutMs != null && (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs))) {
          throw new Error("timeoutMs must be a finite number");
        }
        if (typeof timeoutMs === "number" && timeoutMs <= 0) {
          throw new Error(`timeoutMs must be positive, got ${timeoutMs}`);
        }
        if (typeof timeoutMs === "number" && timeoutMs % 1000 !== 0) {
          throw new Error(`timeoutMs must be a multiple of 1000ms, got ${timeoutMs}`);
        }
        const timeout = (timeoutMs as number | undefined) ?? 300_000;
        if (timeout < 10_000 || timeout > 86_400_000) {
          const require = createRequire(typeof import.meta.url === "string" ? import.meta.url : __filename);
          const { ClientError, Status } = createRequire(require.resolve("modal"))("nice-grpc");
          throw new ClientError("/modal.client.ModalClient/SandboxCreateV2", Status.INVALID_ARGUMENT,
            "Timeout must be between 10s and 86400s (inclusive).");
        }
        return { ...rest, ...options, timeout };
      },
    });
    return guardUnsupported(this, "modal");
  }

  readonly sandboxes = {
    fromName: (...args: Parameters<NativeModalClient["sandboxes"]["fromName"]>) => this.#nativeClient().sandboxes.fromName(...args),
    list: (...args: Parameters<NativeModalClient["sandboxes"]["list"]>) => this.#nativeClient().sandboxes.list(...args),
    experimentalCreate: (...args: Parameters<NativeModalClient["sandboxes"]["experimentalCreate"]>) => this.#nativeClient().sandboxes.experimentalCreate(...args),
    experimentalFromName: (...args: Parameters<NativeModalClient["sandboxes"]["experimentalFromName"]>) => this.#nativeClient().sandboxes.experimentalFromName(...args),
    experimentalFromSnapshot: (...args: Parameters<NativeModalClient["sandboxes"]["experimentalFromSnapshot"]>) => this.#nativeClient().sandboxes.experimentalFromSnapshot(...args),
    experimentalList: (...args: Parameters<NativeModalClient["sandboxes"]["experimentalList"]>) => this.#nativeClient().sandboxes.experimentalList(...args),
    create: async (...args: unknown[]): Promise<Sandbox> => {
      if (args.length > 3) throw new Error("ModalClient.sandboxes.create accepts at most app, image, and options");
      const options = (args.length >= 3 ? args[2] : args.length === 1 ? args[0] : undefined) as Record<string, unknown> | undefined;
      if (args.length === 1 && options && "appId" in options) {
        throw new Error("ModalClient.sandboxes.create requires both app and image");
      }
      assertSupportedObjectKeys(options, MODAL_CREATE_OPTION_KEYS, "modal", "ModalClient.sandboxes.create");
      const handles = args.length >= 2 ? { modalApp: args[0], modalImage: args[1] } : {};
      if (args.length >= 2 && (!args[0] || typeof args[0] !== "object" || !args[1] || typeof args[1] !== "object")) {
        throw new Error("ModalClient.sandboxes.create requires native app and image handles");
      }
      const box = await this.#sdk.sandbox.create({
        ...handles,
        modalCreateParams: options ?? {},
      });
      if (box.provider === "arker") {
        const defaults = creationDefaults(options ?? {});
        if (Object.keys(defaults.env).length || options?.workdir !== undefined) {
          try { await writeDefaults(box, defaults); }
          catch (error) {
            try { await box.destroy(); }
            catch (cleanup) { throw new AggregateError([error, cleanup], `Modal setup and cleanup failed for VM ${box.sandboxId}`); }
            throw error;
          }
        }
      }
      return guardUnsupported(new Sandbox(box), "modal", nativeInstance(box));
    },
    fromId: async (sandboxId: string): Promise<Sandbox> => {
      const box = await this.#sdk.sandbox.getById(sandboxId);
      if (!box) throw new Error(`Sandbox ${sandboxId} not found`);
      return guardUnsupported(new Sandbox(box), "modal", nativeInstance(box));
    },
  };
}
export * from "modal";
