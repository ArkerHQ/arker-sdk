import { validateFirewallStartup } from "./compat/blaxel-packages.js";
import { validateBlaxelNetwork } from "./compat/blaxel-network.js";
import { SandboxInstance as NativeBlaxelSandbox, settings as blaxelSettings } from "@blaxel/core";
import type { VM } from "./index.js";

import { guardUnsupported, type UniversalSandbox } from "./compat/common.js";
import { createCompatSdk, type CompatSdk } from "./compat/runtime.js";
import type { ArkerComputeProviderConfig } from "./compat/arker-provider.js";
import { createBlaxelDataPlane, validateBlaxelNative, type BlaxelDataPlane } from "./compat/blaxel-data-plane.js";
import { blaxelControlCall, blaxelNotFound } from "./compat/blaxel-errors.js";
import { blaxelCreationMetadata, blaxelSandboxMetadata, blaxelImageConfiguration, namedBlaxelFork, validateBlaxelName } from "./compat/blaxel-create.js";
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
  ttl?: string | number;
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
  safe?: boolean;
  createIfNotExist?: boolean;
  timeout?: number;
}

function parseTtl(ttl: string | number | undefined): number | undefined {
  if (ttl === undefined || typeof ttl === "number") return ttl;
  const match = /^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)$/i.exec(ttl.trim());
  if (!match) throw new Error(`Invalid Blaxel ttl '${ttl}'; use milliseconds or a duration such as 30s, 5m, 2h, or 1d`);
  const multiplier = { ms: 1, s: 1_000, m: 60_000, h: 3_600_000, d: 86_400_000 }[match[2]!.toLowerCase()]!;
  return Number(match[1]) * multiplier;
}

function makeSdk(config: BlaxelCompatConfig = {}, getOrCreate = false): CompatSdk {
  return createCompatSdk({
    arker: config.arker,
    arkerFork: (client, request) => blaxelControlCall("create", () => namedBlaxelFork(config.arker, getOrCreate)(client, request)),
    mapArkerCreateOptions: options => {
      validateBlaxelName(options.name);
      const { createIfNotExist: _, network, envs: _envs, labels, ...rest } = options;
      validateBlaxelNetwork(network);
      validateFirewallStartup(options.image, config.arker?.imageConfig);
      validateBlaxelNative();
      const imageConfig = blaxelImageConfiguration(options, config.arker);
      return { ...rest, metadata: blaxelCreationMetadata(labels), ...(imageConfig !== undefined ? { imageConfig } : {}) };
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
  declare readonly drives: NativeBlaxelSandbox["drives"];
  declare readonly snapshots: NativeBlaxelSandbox["snapshots"];
  declare readonly status: NativeBlaxelSandbox["status"];
  declare readonly events: NativeBlaxelSandbox["events"];
  declare readonly spec: NativeBlaxelSandbox["spec"];
  declare readonly lastUsedAt: NativeBlaxelSandbox["lastUsedAt"];
  declare readonly errors: NativeBlaxelSandbox["errors"];
  declare readonly h2Domain: NativeBlaxelSandbox["h2Domain"];
  declare readonly expiresIn: NativeBlaxelSandbox["expiresIn"];
  declare readonly snapshot: NativeBlaxelSandbox["snapshot"];
  declare readonly listSnapshots: NativeBlaxelSandbox["listSnapshots"];
  declare readonly deleteSnapshot: NativeBlaxelSandbox["deleteSnapshot"];
  declare readonly restore: NativeBlaxelSandbox["restore"];
  declare readonly fork: NativeBlaxelSandbox["fork"];
  declare readonly wait: NativeBlaxelSandbox["wait"];
  declare readonly archive: NativeBlaxelSandbox["archive"];
  declare readonly unarchive: NativeBlaxelSandbox["unarchive"];
  #box: UniversalSandbox;
  #native?: NativeBlaxelSandbox;
  #h2Eligible: boolean;
  #dataPlane?: BlaxelDataPlane;

  readonly process: NativeBlaxelSandbox["process"];
  readonly fs: NativeBlaxelSandbox["fs"];

  private constructor(box: UniversalSandbox, native?: NativeBlaxelSandbox, profile = fetchProfile(), dataPlane?: BlaxelDataPlane) {
    this.#box = box;
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
    return guardUnsupported(this, "blaxel", native);
  }

  async fetch(port: number, path = "/", init?: RequestInit): Promise<Response> {
    if (this.#native) return this.#native.fetch(port, path, init);
    const vm = (this.#box as unknown as { getInstance(): VM }).getInstance();
    return privateFetch(vm, port, path, init, this.#h2Eligible && !blaxelSettings.disableH2 ? "blaxel-h2" : "fetch");
  }

  private static async wrap(box: UniversalSandbox, profile?: FetchProfile): Promise<SandboxInstance> {
    if (box.provider !== "blaxel") {
      const vm = (box as UniversalSandbox & { getInstance(): VM }).getInstance();
      await bootstrapBlaxel(vm);
      return new SandboxInstance(box, undefined, profile, createBlaxelDataPlane(vm));
    }
    return new SandboxInstance(box, nativeInstance(box) as NativeBlaxelSandbox);
  }

  static async create(options: BlaxelCreateOptions = {}, callOptions: BlaxelCreateCallOptions = {}): Promise<SandboxInstance> {
    const profile = fetchProfile(options.region);
    const box = await makeSdk(callOptions, callOptions.createIfNotExist ?? false).sandbox.create({
      name: options.name,
      image: options.image,
      memory: options.memory,
      timeout: parseTtl(options.ttl),
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
    try {
      return await SandboxInstance.wrap(box, profile);
    } catch (error) {
      if (box.provider === "arker" && !callOptions.createIfNotExist) {
        try { await box.destroy(); } catch (cleanupError) {
          throw Object.assign(new AggregateError([error, cleanupError], "Sandbox initialization and cleanup failed"),
            { sandboxId: box.sandboxId });
        }
      }
      throw error;
    }
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
    return SandboxInstance.wrap(box);
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
