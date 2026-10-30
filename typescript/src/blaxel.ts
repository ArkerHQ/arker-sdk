import { blaxelDrives, prepareBlaxelDrives, scopedFilesystems } from "./compat/blaxel-drives.js";
import { validateFirewallStartup } from "./compat/blaxel-packages.js";
import { validateBlaxelNetwork } from "./compat/blaxel-network.js";
import { SandboxInstance as NativeBlaxelSandbox, settings as blaxelSettings } from "@blaxel/core";
import type { VM } from "./index.js";

import { guardUnsupported, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";
import { createBlaxelDataPlane, validateBlaxelNative, type BlaxelDataPlane } from "./compat/blaxel-data-plane.js";
import { blaxelControlCall, blaxelNotFound } from "./compat/blaxel-errors.js";
import { blaxelSandboxSpec, blaxelLifetimePolicies, blaxelCreationMetadata, blaxelSandboxMetadata, blaxelImageConfiguration, namedBlaxelFork, validateBlaxelName } from "./compat/blaxel-create.js";
import { bootstrapBlaxel } from "./compat/blaxel-bootstrap.js";
import { privateFetch } from "./compat/private-fetch.js";
import { createNativeBlaxelProvider } from "./compat/blaxel-provider.js";
import { nativeInstance } from "./compat/common.js";

export interface BlaxelCompatConfig {
  apiKey?: string;
  workspace?: string;
  arker?: ArkerComputeProviderConfig;
  routerConfigPath?: string;
}

export interface BlaxelCreateOptions {
  name?: string;
  image?: string;
  memory?: number;
  ttl?: string | null;
  envs?: Record<string, string>;
  runtime?: string;
  region?: string;
  ports?: Array<{ target: number; protocol?: string }>;
  labels?: Record<string, string>;
  network?: NonNullable<NativeBlaxelSandbox["spec"]>["network"];
  safe?: boolean;
  timeout?: number;
}

export interface BlaxelCreateCallOptions extends BlaxelCompatConfig {
  mountDrives?: NonNullable<Parameters<typeof NativeBlaxelSandbox.create>[1]>["mountDrives"];
  safe?: boolean;
  createIfNotExist?: boolean;
  timeout?: number;
}

function makeSdk(config: BlaxelCompatConfig = {}, getOrCreate = false, ttl?: unknown, onDriveSetup?: (setup: ReturnType<typeof prepareBlaxelDrives>) => void): CompatSdk {
  return createCompatSdk({
    arker: config.arker,
    arkerFork: (client, request) => {
      const lifetime = blaxelLifetimePolicies(ttl, config.arker?.policies);
      if (lifetime) request = {...request, policies: {...request.policies, rules: {...request.policies?.rules, ...lifetime.rules}}};
      return blaxelControlCall("create", () => namedBlaxelFork(config.arker, getOrCreate)(client, request));
    },
    mapArkerCreateOptions: options => {
      if (options.mountDrives !== undefined) {
        const setup = prepareBlaxelDrives(options.mountDrives, {createIfNotExist: getOrCreate, region: options.region as string | undefined});
        if ((options.mountDrives as unknown[]).length) onDriveSetup?.(setup);
      }
      validateBlaxelName(options.name);
      const { createIfNotExist: _, ttl: _ttl, network, envs: _envs, labels, mountDrives: _mountDrives, ...rest } = options;
      validateBlaxelNetwork(network);
      validateFirewallStartup(options.image, config.arker?.imageConfig);
      validateBlaxelNative();
      const imageConfig = blaxelImageConfiguration(options, config.arker);
      return { ...rest, metadata: blaxelCreationMetadata(labels, _ttl), ...(imageConfig !== undefined ? { imageConfig } : {}) };
    },
    fallbackProvider: createNativeBlaxelProvider({
      apiKey: config.apiKey,
      workspace: config.workspace,
    }),
    routerConfigPath: config.routerConfigPath,
  });
}

async function deleteArkerBox(box: UniversalSandbox): Promise<void> {
  const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
  const result = await blaxelControlCall("delete", () => vm.delete());
  if (!result.deleted) throw blaxelNotFound();
}

interface FetchProfile {
  region?: string;
  disableH2: boolean;
}

function fetchProfile(region?: string): FetchProfile {
  return { region: region || blaxelSettings.region, disableH2: blaxelSettings.disableH2 };
}

export class SandboxInstance {
  readonly id: string;
  readonly provider: string;
  readonly metadata: NativeBlaxelSandbox["metadata"];
  readonly network: NativeBlaxelSandbox["network"];
  declare readonly previews: NativeBlaxelSandbox["previews"];
  declare readonly schedules: NativeBlaxelSandbox["schedules"];
  declare readonly sessions: NativeBlaxelSandbox["sessions"];
  declare readonly codegen: NativeBlaxelSandbox["codegen"];
  declare readonly system: NativeBlaxelSandbox["system"];
  declare readonly snapshots: NativeBlaxelSandbox["snapshots"];
  declare readonly status: NativeBlaxelSandbox["status"];
  declare readonly events: NativeBlaxelSandbox["events"];

  declare readonly lastUsedAt: NativeBlaxelSandbox["lastUsedAt"];
  declare readonly errors: NativeBlaxelSandbox["errors"];
  declare readonly h2Domain: NativeBlaxelSandbox["h2Domain"];
  declare readonly expiresIn: NativeBlaxelSandbox["expiresIn"];
  declare readonly snapshot: NativeBlaxelSandbox["snapshot"];
  declare readonly listSnapshots: NativeBlaxelSandbox["listSnapshots"];
  declare readonly deleteSnapshot: NativeBlaxelSandbox["deleteSnapshot"];
  declare readonly restore: NativeBlaxelSandbox["restore"];

  declare readonly wait: NativeBlaxelSandbox["wait"];
  declare readonly archive: NativeBlaxelSandbox["archive"];
  declare readonly unarchive: NativeBlaxelSandbox["unarchive"];
  #box: UniversalSandbox;
  #forkConfig: ArkerComputeProviderConfig;
  #native?: NativeBlaxelSandbox;
  #h2Eligible: boolean;
  #dataPlane?: BlaxelDataPlane;

  readonly process: NativeBlaxelSandbox["process"];
  readonly fs: NativeBlaxelSandbox["fs"];
  readonly drives: NativeBlaxelSandbox["drives"];

  private constructor(box: UniversalSandbox, native?: NativeBlaxelSandbox, profile = fetchProfile(), dataPlane?: BlaxelDataPlane, config: ArkerComputeProviderConfig = {}) {
    this.#box = box;
    this.#forkConfig = {...config};
    this.#native = native;
    this.#dataPlane = dataPlane;
    this.#h2Eligible = Boolean(profile.region) && !profile.disableH2;
    const network = guardUnsupported({ fetch: this.fetch.bind(this) }, "blaxel");
    this.network = native?.network ?? network as unknown as NativeBlaxelSandbox["network"];
    this.id = box.sandboxId;
    this.provider = box.provider;
    const vm = box.provider === "arker" ? (box as UniversalSandbox & { getInstance(): VM }).getInstance() : undefined;
    this.metadata = (native?.metadata ?? (vm ? blaxelSandboxMetadata(vm) : { name: box.sandboxId })) as NativeBlaxelSandbox["metadata"];
    if (!native && !dataPlane) throw new Error("Native Blaxel data plane was not initialized");
    this.process = (native ?? dataPlane!).process;
    this.fs = (native ?? dataPlane!).fs;
    this.drives = (native ?? dataPlane!).drives;
    if (vm) {
      const bindings = () => blaxelDrives(vm, scopedFilesystems(vm));
      this.drives.mount = request => bindings().mount(request);
      this.drives.list = () => bindings().list();
      this.drives.unmount = path => bindings().unmount(path);
    }
    return guardUnsupported(this, "blaxel", native);
  }

  get spec(): NativeBlaxelSandbox["spec"] {
    if (this.#native) return this.#native.spec;
    return blaxelSandboxSpec((this.#box as UniversalSandbox & {getInstance(): VM}).getInstance());
  }

  async fork(...[targetName, options = {}]: Parameters<NativeBlaxelSandbox["fork"]>): ReturnType<NativeBlaxelSandbox["fork"]> {
    if (this.#native) return this.#native.fork(targetName, options);
    if ((options.targetType ?? "sandbox") !== "sandbox"
        || Object.entries(options).some(([key, value]) => key !== "targetType" && value !== undefined)) {
      throw new Error("Unsupported Blaxel fork options: Arker supports live sandbox forks without additional options");
    }
    if (!targetName) throw {code: 400, error: "A fork target name is required"};
    const vm = (this.#box as UniversalSandbox & {getInstance(): VM}).getInstance();
    const child = await blaxelControlCall("fork", () => namedBlaxelFork(this.#forkConfig, false, "fork")(vm._client, {source_vm_id: vm.id, name: targetName}));
    return {name: child.name ?? undefined, type: "sandbox", snapshotId: ""};
  }

  async fetch(port: number, path = "/", init?: RequestInit): Promise<Response> {
    if (this.#native) return this.#native.fetch(port, path, init);
    const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
    return privateFetch(vm, port, path, init, this.#h2Eligible && !blaxelSettings.disableH2 ? "blaxel-h2" : "fetch");
  }

  private static async wrap(box: UniversalSandbox, profile?: FetchProfile, config?: ArkerComputeProviderConfig): Promise<SandboxInstance> {
    if (box.provider !== "blaxel") {
      const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
      await bootstrapBlaxel(vm);
      return new SandboxInstance(box, undefined, profile, createBlaxelDataPlane(vm), config);
    }
    return new SandboxInstance(box, nativeInstance(box) as NativeBlaxelSandbox);
  }

  static async create(options: BlaxelCreateOptions = {}, callOptions: BlaxelCreateCallOptions = {}): Promise<SandboxInstance> {
    const profile = fetchProfile(options.region);
    let driveSetup: ReturnType<typeof prepareBlaxelDrives> | undefined;
    const box = await makeSdk(callOptions, callOptions.createIfNotExist ?? false, options.ttl, setup => {driveSetup = setup;}).sandbox.create({
      ...(callOptions.mountDrives !== undefined ? {mountDrives: callOptions.mountDrives} : {}),
      name: options.name,
      image: options.image,
      memory: options.memory,
      ...(Object.hasOwn(options, "ttl") ? {ttl: options.ttl} : {}),
      envs: options.envs,
      runtime: options.runtime,
      ...(options.region !== undefined ? { region: options.region } : {}),
      ...(options.ports !== undefined ? { ports: options.ports } : {}),
      ...(options.labels !== undefined ? { labels: options.labels } : {}),
      ...(options.network !== undefined ? { network: options.network } : {}),
      ...((callOptions.safe ?? options.safe) !== undefined ? { safe: callOptions.safe ?? options.safe } : {}),
      createIfNotExist: callOptions.createIfNotExist ?? false,
      ...((callOptions.timeout ?? options.timeout) !== undefined
        ? { creationTimeout: callOptions.timeout ?? options.timeout }
        : {}),
    });
    let instance: SandboxInstance;
    try {
      instance = await SandboxInstance.wrap(box, profile, callOptions.arker);
    } catch (error) {
      if (box.provider === "arker" && !callOptions.createIfNotExist) {
        try { await box.destroy(); } catch (cleanupError) {
          throw Object.assign(new AggregateError([error, cleanupError], "Sandbox initialization and cleanup failed"),
            { sandboxId: box.sandboxId });
        }
      }
      throw error;
    }
    if (driveSetup) {
      const vm = (box as UniversalSandbox & {getInstance(): VM}).getInstance();
      await driveSetup.mount(instance as unknown as NativeBlaxelSandbox, vm, scopedFilesystems(vm));
    }
    return instance;
  }

  static async createIfNotExists(
    options: BlaxelCreateOptions = {},
    callOptions: Omit<BlaxelCreateCallOptions, "createIfNotExist"> = {},
  ): Promise<SandboxInstance> {
    return SandboxInstance.create(options, { ...callOptions, createIfNotExist: true });
  }

  static async get(id: string, config: BlaxelCompatConfig = {}): Promise<SandboxInstance> {
    const box = await blaxelControlCall("get", () => makeSdk(config).sandbox.getById(id));
    if (!box) throw blaxelNotFound();
    return SandboxInstance.wrap(box, undefined, config.arker);
  }

  static async delete(id: string, config: BlaxelCompatConfig = {}): Promise<void> {
    const box = await blaxelControlCall("get", () => makeSdk(config).sandbox.getById(id));
    if (!box) throw blaxelNotFound();
    if (box.provider === "arker") await deleteArkerBox(box);
    else await (nativeInstance(box) as NativeBlaxelSandbox).delete();
  }

  async delete(): Promise<void> {
    try {
      if (this.#native) await this.#native.delete();
      else await deleteArkerBox(this.#box);
    } finally { this.#dataPlane?.close(); }
  }
}

export class Blaxel {
  constructor(private readonly config: BlaxelCompatConfig = {}) {}
  create(options: BlaxelCreateOptions = {}) { return SandboxInstance.create(options, this.config); }
  get(id: string) { return SandboxInstance.get(id, this.config); }
  delete(id: string) { return SandboxInstance.delete(id, this.config); }
}
export * from "@blaxel/core";

export { DriveInstance, VolumeInstance } from "./compat/blaxel-resources.js";
