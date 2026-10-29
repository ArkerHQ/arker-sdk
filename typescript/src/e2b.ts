import { listSandboxes, type E2BListOptions, type E2BPaginator } from "./compat/e2b-list.js";
import { e2bForkMetadata } from "./compat/e2b-metadata.js";
import { configureE2BIngress, validateE2BNetwork, type E2BNetworkOptions } from "./compat/e2b-network.js";
import { E2BLifecycle } from "./compat/e2b-lifecycle.js";
import { E2BFileURLs, type E2BFileURLOptions } from "./compat/e2b-urls.js";
import { bootstrapEnvd, validateEnvdEnvs } from "./compat/e2b-bootstrap.js";
import { createE2BDataPlane, type E2BDataPlane, type EnvdDescriptor } from "./compat/e2b-data-plane.js";
import { ArkerError, type VM } from "./index.js";
import { Sandbox as VendorSandbox, SandboxNotFoundError, type Commands, type Filesystem, type Pty, type Git, type SandboxConnectOpts, type SandboxPauseOpts } from "e2b";
import {
  assertSupportedObjectKeys, guardUnsupported, nativeInstance, resolveTimeout,
  type CreateOptions, type UniversalSandbox,
} from "./compat/common.js";
import { createCompatSdk, createCompatRouter } from "./compat/runtime.js";
import { createNativeE2BProvider } from "./compat/e2b-provider.js";
import { checkMountBackend, mountReadyCommand, mountStore, type E2BVolumeMounts, type VolumeMounts } from "./compat/e2b-mounts.js";
export { Volume } from "./compat/e2b-mounts.js";
export type { E2BVolumeMounts } from "./compat/e2b-mounts.js";
export type E2BCreateOptions = CreateOptions & { volumeMounts?: E2BVolumeMounts; network?: E2BNetworkOptions };

export type { CommandResult as CommandExitResult } from "e2b";

const descriptors = new WeakMap<E2BDataPlane, EnvdDescriptor>();

type NativeSandbox = Pick<VendorSandbox, "kill" | "getHost" | "commands" | "files" | "git" | "pty">;
const E2B_CREATE_OPTION_KEYS = ["timeout", "timeoutMs", "routerConfigPath", "envs", "metadata", "allowInternetAccess", "volumeMounts", "network"] as const;

function routingOptions(routerConfigPath?: string, volumeMounts?: E2BVolumeMounts) {
  const fallbackProvider = createNativeE2BProvider();
  if (volumeMounts !== undefined) {
    const create = fallbackProvider.sandbox.create.bind(fallbackProvider.sandbox);
    fallbackProvider.sandbox.create = (options) => {
      checkMountBackend(volumeMounts, "e2b");
      return create(options);
    };
  }
  return {
    fallbackProvider,
    routerConfigPath,
    mapArkerForkMetadata: e2bForkMetadata,
    mapArkerCreateOptions: (values: Record<string, unknown>) => {
      validateEnvdEnvs(values.envs as Record<string, string> | undefined);
      validateE2BNetwork(values.network as E2BNetworkOptions | undefined);
      if (volumeMounts !== undefined) checkMountBackend(volumeMounts, "arker");
      const { volumeMounts: _mounts, network: _network, envs: _envs, ...rest } = values;
      return rest;
    },
  };
}

function sdk(routerConfigPath?: string, volumeMounts?: E2BVolumeMounts) {
  return createCompatSdk(routingOptions(routerConfigPath, volumeMounts));
}

export class Sandbox {
  readonly sandboxId: string;
  readonly provider: string;
  #box: UniversalSandbox;
  #native?: NativeSandbox;
  #arkerHostname?: string;
  #dataPlane?: E2BDataPlane;

  private components(): NativeSandbox | E2BDataPlane {
    const components = this.#native ?? this.#dataPlane;
    if (!components) throw new Error("Sandbox data plane is not initialized");
    return components;
  }

  get commands(): Commands { return this.components().commands; }
  get files(): Filesystem { return this.components().files; }
  get git(): Git { return this.components().git; }
  get pty(): Pty { return this.components().pty; }

  private constructor(box: UniversalSandbox, native?: object, arkerHostname?: string, dataPlane?: E2BDataPlane) {
    this.#box = box;
    this.#native = native as NativeSandbox | undefined;
    this.#arkerHostname = arkerHostname;
    this.#dataPlane = dataPlane;
    this.sandboxId = box.sandboxId;
    this.provider = box.provider;
  }

  private static async dataPlane(box: UniversalSandbox, envs?: Record<string, string>): Promise<E2BDataPlane> {
    const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
    const authorization = vm._client._authHeaders().authorization;
    if (!authorization?.startsWith("Bearer ")) throw new Error("Missing Arker transport credentials");
    const [descriptor] = await bootstrapEnvd(vm, { envs });
    const plane = await createE2BDataPlane(descriptor, authorization.slice(7));
    descriptors.set(plane, descriptor);
    return plane;
  }

  private static async wrap(
    box: UniversalSandbox,
    options: { deleteOnValidationFailure?: boolean; initializeDataPlane?: boolean; envs?: Record<string, string> } = {},
  ): Promise<Sandbox> {
    const native = nativeInstance(box);
    let dataPlane: E2BDataPlane | undefined;
    try {
      if (!native && box.provider === "arker") {
        if (options.initializeDataPlane !== false) dataPlane = await Sandbox.dataPlane(box, options.envs);
      }
      const arkerHostname = box.provider === "arker"
        ? await box.getUrl({ port: 8080 }).then((url) => new URL(url).hostname).catch(() => undefined)
        : undefined;
      return guardUnsupported(new Sandbox(box, native, arkerHostname, dataPlane), "e2b", native);
    } catch (error) {
      dataPlane?.close();
      if (options.deleteOnValidationFailure && box.provider === "arker") {
        const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
        try { await vm.delete(); }
        catch (cleanupError) { throw new AggregateError([error, cleanupError], "Sandbox initialization and cleanup failed"); }
      }
      throw error;
    }
  }

  static async create(opts?: E2BCreateOptions): Promise<Sandbox>;
  static async create(templateId: string | undefined, opts?: E2BCreateOptions): Promise<Sandbox>;
  static async create(templateOrOptions?: string | E2BCreateOptions, options?: E2BCreateOptions): Promise<Sandbox> {
    const templateId = typeof templateOrOptions === "string" ? templateOrOptions : undefined;
    const opts = typeof templateOrOptions === "string" ? options : templateOrOptions ?? options;
    assertSupportedObjectKeys(opts, E2B_CREATE_OPTION_KEYS, "e2b", "Sandbox.create");
    const timeout = resolveTimeout(opts) ?? 300_000;
    const box = await sdk(opts?.routerConfigPath, opts?.volumeMounts).sandbox.create({
      ...(opts?.volumeMounts !== undefined ? { volumeMounts: opts.volumeMounts } : {}),
      ...(templateId ? { templateId } : {}),
      timeout,
      ...(opts?.network !== undefined ? { network: opts.network } : {}),
      ...((opts as any)?.envs ? { envs: (opts as any).envs } : {}),
      metadata: (opts as any)?.metadata === undefined ? {} : (opts as any).metadata,
      ...((opts as any)?.allowInternetAccess !== undefined ? { allowInternetAccess: (opts as any).allowInternetAccess } : {}),
    });
    if (box.provider === "arker") {
      const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
      try { await configureE2BIngress(vm, opts?.network?.allowPublicTraffic ?? true); }
      catch (error) {
        try { await vm.delete(); }
        catch (cleanupError) { throw new AggregateError([error, cleanupError], "Sandbox network initialization and cleanup failed"); }
        throw error;
      }
    }
    if (box.provider !== "arker" || !opts?.volumeMounts || !Object.keys(opts.volumeMounts).length) {
      return Sandbox.wrap(box, { deleteOnValidationFailure: true, envs: (opts as any)?.envs });
    }
    const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
    let sandbox: Sandbox | undefined;
    try {
      sandbox = await Sandbox.wrap(box, { envs: (opts as any)?.envs });
      checkMountBackend(opts.volumeMounts, "arker");
      await mountStore(vm._client, opts.volumeMounts).attach(vm, opts.volumeMounts as VolumeMounts);
      const ready = await sandbox.commands.run(mountReadyCommand(Object.keys(opts.volumeMounts), timeout / 1000), { timeoutMs: timeout });
      if (ready.exitCode !== 0) throw new Error("Volume mount readiness failed; sandbox was not returned");
      return sandbox;
    } catch (error) {
      sandbox?.closeDataPlane();
      try {
        await vm.delete();
      } catch (cleanupError) {
        throw new AggregateError([error, cleanupError], `Sandbox creation and VM cleanup failed; delete VM ${vm.id} explicitly`);
      }
      throw error;
    }
  }

  static async connect(sandboxId: string, opts?: SandboxConnectOpts & Pick<CreateOptions, "routerConfigPath">): Promise<Sandbox> {
    const { routerConfigPath, ...options } = opts ?? {};
    const owner = await createCompatRouter(routingOptions(routerConfigPath)).getOwner(sandboxId);
    if (!owner) throw new Error(`Sandbox ${sandboxId} not found`);
    if (owner.provider === "e2b") {
      const native = await VendorSandbox.connect(sandboxId, options);
      const box = { provider: "e2b", sandboxId: native.sandboxId, getInstance: () => native } as unknown as UniversalSandbox;
      return Sandbox.wrap(box);
    }
    const box = owner.instance as UniversalSandbox;
    await new E2BLifecycle(box).connect(options);
    return Sandbox.wrap(box);
  }

  private closeDataPlane(): void {
    this.#dataPlane?.close();
  }

  private lifecycle(): E2BLifecycle {
    return new E2BLifecycle(this.#box, this.#native as VendorSandbox | undefined);
  }

  async connect(opts?: SandboxConnectOpts): Promise<this> {
    await this.lifecycle().connect(opts);
    if (!this.#native) {
      const replacement = await Sandbox.dataPlane(this.#box);
      const previous = this.#dataPlane;
      this.#dataPlane = replacement;
      previous?.close();
    }
    return this;
  }

  pause(opts?: SandboxPauseOpts): Promise<boolean> {
    return this.lifecycle().pause(opts);
  }

  betaPause(opts?: SandboxPauseOpts): Promise<boolean> {
    return this.lifecycle().betaPause(opts);
  }

  setTimeout(timeoutMs: number, opts?: Parameters<VendorSandbox["setTimeout"]>[1]): Promise<void> {
    return this.lifecycle().setTimeout(timeoutMs, opts);
  }

  isRunning(opts?: Parameters<VendorSandbox["isRunning"]>[0]): Promise<boolean> {
    return this.lifecycle().isRunning(opts);
  }

  getInfo(opts?: Parameters<VendorSandbox["getInfo"]>[0]): ReturnType<VendorSandbox["getInfo"]> {
    return this.lifecycle().getInfo(opts);
  }

  static list(opts?: E2BListOptions): E2BPaginator {
    return listSandboxes(opts);
  }

  static async getInfo(sandboxId: string, opts?: Parameters<typeof VendorSandbox.getInfo>[1] & Pick<CreateOptions, "routerConfigPath">): ReturnType<VendorSandbox["getInfo"]> {
    const owner = await createCompatRouter(routingOptions(opts?.routerConfigPath)).getOwner(sandboxId);
    if (!owner) throw new SandboxNotFoundError(`Sandbox ${sandboxId} not found`);
    const { routerConfigPath: _router, ...options } = opts ?? {};
    if (owner.provider === "e2b") return VendorSandbox.getInfo(sandboxId, options);
    return new E2BLifecycle(owner.instance as UniversalSandbox).getInfo(options);
  }

  static async kill(sandboxId: string, opts?: Pick<CreateOptions, "routerConfigPath">): Promise<boolean> {
    assertSupportedObjectKeys(opts, ["routerConfigPath"], "e2b", "Sandbox.kill");
    const owner = await createCompatRouter(routingOptions(opts?.routerConfigPath)).getOwner(sandboxId);
    if (!owner) return false;
    if (owner.provider === "e2b") return VendorSandbox.kill(sandboxId);
    return (await Sandbox.wrap(owner.instance, { initializeDataPlane: false })).kill();
  }

  downloadUrl(path: string, options?: E2BFileURLOptions): Promise<string> {
    if (this.#native) return (this.#native as unknown as VendorSandbox).downloadUrl(path, options);
    const vm = (this.#box as UniversalSandbox & { getInstance(): VM }).getInstance();
    return new E2BFileURLs(vm, undefined, this.#dataPlane && descriptors.get(this.#dataPlane)).downloadUrl(path, options);
  }

  uploadUrl(path?: string, options?: E2BFileURLOptions): Promise<string> {
    if (this.#native) return (this.#native as unknown as VendorSandbox).uploadUrl(path, options);
    const vm = (this.#box as UniversalSandbox & { getInstance(): VM }).getInstance();
    return new E2BFileURLs(vm, undefined, this.#dataPlane && descriptors.get(this.#dataPlane)).uploadUrl(path, options);
  }

  getHost(port: number): string {
    if (this.#native?.getHost) return this.#native.getHost(port);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("port must be an integer from 1 through 65535");
    if (!this.#arkerHostname) throw new Error("Arker did not return an inbound hostname for this sandbox");
    return port === 8080 ? this.#arkerHostname : this.#arkerHostname.replace(/^([^.]+)/, `$1-${port}`);
  }

  async kill(): Promise<boolean> {
    if (this.#native) return this.#native.kill();
    const vm = (this.#box as UniversalSandbox & { getInstance(): VM }).getInstance();
    try {
      await vm.refresh();
      const deleted = (await vm.delete()).deleted;
      return deleted;
    } catch (error) {
      if (error instanceof ArkerError && error.status === 404) return false;
      throw error;
    } finally {
      this.#dataPlane?.close();
    }
  }
}
export * from "e2b";
