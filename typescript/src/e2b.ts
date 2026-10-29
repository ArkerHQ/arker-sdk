import { forkArker } from "./compat/e2b-fork.js";
import { ArkerComputeSandbox } from "./compat/arker-provider.js";
import { listSandboxes, type E2BListOptions, type E2BPaginator } from "./compat/e2b-list.js";
import { e2bForkMetadata } from "./compat/e2b-metadata.js";
import { forkWithTraffic, trafficToken, validateE2BNetwork, type E2BNetworkOptions } from "./compat/e2b-network.js";
import { E2BLifecycle, lifecycleTransport, e2bTimeoutDuration } from "./compat/e2b-lifecycle.js";
import { E2BFileURLs, prepareFileURLs, type E2BFileURLOptions } from "./compat/e2b-urls.js";
import { bootstrapEnvd, validateEnvdEnvs } from "./compat/e2b-bootstrap.js";
import { createE2BDataPlane, type E2BDataPlane, type EnvdDescriptor } from "./compat/e2b-data-plane.js";
import type { VM } from "./index.js";
import { Sandbox as VendorSandbox, SandboxNotFoundError, type Commands, type Filesystem, type Pty, type Git, type SandboxConnectOpts, type SandboxPauseOpts, type SandboxOpts, type SandboxApiOpts, type SandboxForkOpts } from "e2b";
import {
  assertSupportedObjectKeys, guardUnsupported, nativeInstance, resolveTimeout,
  type CreateOptions, type UniversalSandbox,
} from "./compat/common.js";
import { createCompatSdk, createCompatRouter } from "./compat/runtime.js";
import { createNativeE2BProvider } from "./compat/e2b-provider.js";
import { checkMountBackend, mountReadyCommand, mountStore, type E2BVolumeMounts, type VolumeMounts } from "./compat/e2b-mounts.js";
export { Volume } from "./compat/e2b-mounts.js";
export type { E2BVolumeMounts } from "./compat/e2b-mounts.js";
export type E2BCreateOptions = Omit<SandboxOpts, "volumeMounts"> & CreateOptions & { volumeMounts?: E2BVolumeMounts };

export type { CommandResult as CommandExitResult, SandboxForkOpts } from "e2b";

const descriptors = new WeakMap<E2BDataPlane, EnvdDescriptor>();

type NativeSandbox = Pick<VendorSandbox, "fork" | "kill" | "getHost" | "commands" | "files" | "git" | "pty" | "trafficAccessToken">;
const E2B_CREATE_OPTION_KEYS = ["timeout", "timeoutMs", "routerConfigPath", "envs", "metadata", "allowInternetAccess", "volumeMounts", "network", "template", "apiKey", "secure", "validateApiKey", "debug", "lifecycle"] as const;

function routingOptions(routerConfigPath?: string, volumeMounts?: E2BVolumeMounts, lookupOptions?: SandboxApiOpts) {
  const { mode: _mode, keepMemory: _keepMemory, onResume: _onResume, timeoutMs: _timeout, ...apiOptions } =
    (lookupOptions ?? {}) as SandboxPauseOpts & SandboxConnectOpts;
  let network: E2BNetworkOptions | undefined;
  const fallbackProvider = createNativeE2BProvider(apiOptions);
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
    arkerLookupTransport: lookupOptions ? lifecycleTransport(lookupOptions) : undefined,
    mapArkerForkMetadata: (request: import("./index.js").ForkOptions, metadata: unknown) => e2bForkMetadata(request, metadata, network),
    arkerFork: (client: import("./index.js").Arker, request: import("./index.js").ForkOptions) => forkWithTraffic(client, request, network?.allowPublicTraffic ?? true),
    mapArkerCreateOptions: (values: Record<string, unknown>) => {
      assertSupportedObjectKeys(values, [...E2B_CREATE_OPTION_KEYS, "templateId"], "e2b", "Sandbox.create");
      if (values.debug !== undefined && values.debug !== false) throw new Error("Arker Sandbox.create supports only debug=false");
      if (values.lifecycle !== undefined) {
        const lifecycle = values.lifecycle as SandboxOpts["lifecycle"];
        if (!lifecycle || typeof lifecycle !== "object" || Array.isArray(lifecycle)) throw new Error("Arker Sandbox.create supports only kill lifecycle");
        assertSupportedObjectKeys(lifecycle, ["onTimeout", "autoResume"], "e2b", "Sandbox.create lifecycle");
        const action = lifecycle.onTimeout ?? "kill";
        if (action !== "kill" && !(typeof action === "object" && action.action === "kill" && Object.keys(action).length === 1)) {
          throw new Error("Arker Sandbox.create supports only kill lifecycle");
        }
        if (lifecycle.autoResume !== undefined && lifecycle.autoResume !== false) throw new Error("Arker Sandbox.create supports only autoResume=false");
      }
      validateEnvdEnvs(values.envs as Record<string, string> | undefined);
      validateE2BNetwork(values.network as E2BNetworkOptions | undefined);
      network = values.network as E2BNetworkOptions | undefined;
      if (volumeMounts !== undefined) checkMountBackend(volumeMounts, "arker");
      const { volumeMounts: _mounts, network: _network, envs: _envs, timeoutMs: _timeoutMs,
        apiKey: _apiKey, secure: _secure, validateApiKey: _validateApiKey, debug: _debug, lifecycle: _lifecycle, template, ...rest } = values;
      return { ...rest, ...(template !== undefined && rest.templateId === undefined ? { templateId: template } : {}), timeout: e2bTimeoutDuration(resolveTimeout(values as CreateOptions) ?? 300_000) };
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

  get trafficAccessToken(): string | undefined {
    return this.#native ? this.#native.trafficAccessToken
      : trafficToken((this.#box as UniversalSandbox & { getInstance(): VM }).getInstance());
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
    await prepareFileURLs(vm);
    const plane = await createE2BDataPlane(descriptor, authorization.slice(7));
    descriptors.set(plane, descriptor);
    return plane;
  }

  private static async wrap(
    box: UniversalSandbox,
    options: { deleteOnValidationFailure?: boolean; envs?: Record<string, string> } = {},
  ): Promise<Sandbox> {
    const native = nativeInstance(box);
    let dataPlane: E2BDataPlane | undefined;
    try {
      if (!native && box.provider === "arker") {
        dataPlane = await Sandbox.dataPlane(box, options.envs);
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
    const timeout = resolveTimeout(opts) ?? 300_000;
    const { routerConfigPath, ...nativeOptions } = opts ?? {};
    const box = await sdk(routerConfigPath, opts?.volumeMounts).sandbox.create({
      ...nativeOptions,
      ...(templateId !== undefined ? { templateId } : {}),
    });
    if (box.provider !== "arker" || !opts?.volumeMounts || !Object.keys(opts.volumeMounts).length) {
      return Sandbox.wrap(box, { deleteOnValidationFailure: true, envs: opts?.envs });
    }
    const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
    let sandbox: Sandbox | undefined;
    try {
      sandbox = await Sandbox.wrap(box, { envs: opts?.envs });
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

  private static async wrapForks(values: Array<VendorSandbox | Error>): Promise<Array<Sandbox | Error>> {
    return Promise.all(values.map(value => value instanceof Error ? value : Sandbox.wrap({
      provider: "e2b", sandboxId: value.sandboxId, getInstance: () => value,
    } as unknown as UniversalSandbox)));
  }

  static async fork(sandboxId: string, opts: SandboxForkOpts & Pick<CreateOptions, "routerConfigPath"> = {}): Promise<Array<Sandbox | Error>> {
    const { routerConfigPath, ...options } = opts;
    const owner = await createCompatRouter(routingOptions(routerConfigPath, undefined, options)).getOwner(sandboxId);
    if (!owner) throw new SandboxNotFoundError(`Sandbox ${sandboxId} not found`);
    if (owner.provider === "e2b") return Sandbox.wrapForks(await VendorSandbox.fork(sandboxId, options));
    return new Sandbox(owner.instance as UniversalSandbox).fork(options);
  }

  async fork(opts?: SandboxForkOpts): Promise<Array<Sandbox | Error>> {
    if (this.#native) return Sandbox.wrapForks(await this.#native.fork(opts));
    const vm = (this.#box as UniversalSandbox & { getInstance(): VM }).getInstance();
    return forkArker(vm, child => Sandbox.wrap(new ArkerComputeSandbox(child, async () => { await child.delete(); })),
      async child => {
        const failures: unknown[] = [];
        try { child.closeDataPlane(); } catch (error) { failures.push(error); }
        try { await (child.#box as UniversalSandbox & { getInstance(): VM }).getInstance().delete(); }
        catch (error) { failures.push(error); }
        if (failures.length) throw new AggregateError(failures, "Fork client cleanup failed");
      }, opts);
  }

  static async connect(sandboxId: string, opts?: SandboxConnectOpts & Pick<CreateOptions, "routerConfigPath">): Promise<Sandbox> {
    const { routerConfigPath, ...options } = opts ?? {};
    const owner = await createCompatRouter(routingOptions(routerConfigPath, undefined, options)).getOwner(sandboxId);
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

  private static async control<T>(
    sandboxId: string, opts: (SandboxApiOpts & Pick<CreateOptions, "routerConfigPath">) | undefined,
    native: (options: SandboxApiOpts) => Promise<T>,
    arker: (lifecycle: E2BLifecycle, options: SandboxApiOpts) => Promise<T>,
    missing?: () => T,
  ): Promise<T> {
    const { routerConfigPath, ...options } = opts ?? {};
    const owner = await createCompatRouter(routingOptions(routerConfigPath, undefined, options)).getOwner(sandboxId);
    if (!owner) {
      if (missing) return missing();
      throw new SandboxNotFoundError(`Sandbox ${sandboxId} not found`);
    }
    return owner.provider === "e2b" ? native(options) : arker(new E2BLifecycle(owner.instance as UniversalSandbox), options);
  }

  static getInfo(sandboxId: string, opts?: SandboxApiOpts & Pick<CreateOptions, "routerConfigPath">): ReturnType<VendorSandbox["getInfo"]> {
    return this.control(sandboxId, opts, options => VendorSandbox.getInfo(sandboxId, options), (lifecycle, options) => lifecycle.getInfo(options));
  }

  static setTimeout(sandboxId: string, timeoutMs: number, opts?: SandboxApiOpts & Pick<CreateOptions, "routerConfigPath">): Promise<void> {
    return this.control(sandboxId, opts, options => VendorSandbox.setTimeout(sandboxId, timeoutMs, options), (lifecycle, options) => lifecycle.setTimeout(timeoutMs, options));
  }

  static pause(sandboxId: string, opts?: SandboxPauseOpts & Pick<CreateOptions, "routerConfigPath">): Promise<boolean> {
    return this.control(sandboxId, opts, options => VendorSandbox.pause(sandboxId, options), (lifecycle, options) => lifecycle.pause(options));
  }

  static betaPause(sandboxId: string, opts?: SandboxPauseOpts & Pick<CreateOptions, "routerConfigPath">): Promise<boolean> {
    return this.pause(sandboxId, opts);
  }

  static kill(sandboxId: string, opts?: SandboxApiOpts & Pick<CreateOptions, "routerConfigPath">): Promise<boolean> {
    return this.control(sandboxId, opts, options => VendorSandbox.kill(sandboxId, options), (lifecycle, options) => lifecycle.kill(options), () => false);
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

  async kill(opts?: Parameters<VendorSandbox["kill"]>[0]): Promise<boolean> {
    if (this.#native) return this.#native.kill(opts);
    try { return await this.lifecycle().kill(opts); }
    finally { this.closeDataPlane(); }
  }
}
export * from "e2b";
