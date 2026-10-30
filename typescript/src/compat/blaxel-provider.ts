import { SandboxInstance, initialize, settings } from "@blaxel/core";
import {
  defineProvider,
  createConfigGate,
  createConfigStamp,
} from "@computesdk/provider";
import { onMissing } from "./common.js";

type Config = {
  apiKey?: string;
  workspace?: string;
  clientCredentials?: Parameters<typeof initialize>[0]["clientCredentials"];
};
const gate = createConfigGate();
const stamp = createConfigStamp<Config>();

function configured<T>(
  config: Config,
  operation: () => Promise<T>,
): Promise<T> {
  return gate.withConfig(
    JSON.stringify(config),
    () => {
      const {
        apiKey: _key,
        apikey: _legacyKey,
        workspace: _workspace,
        clientCredentials: _clientCredentials,
        ...runtime
      } = settings.config;
      initialize({
        ...runtime,
        apiKey: config.apiKey,
        workspace: config.workspace,
        clientCredentials: config.clientCredentials,
      });
    },
    operation,
  );
}

function missing(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as {
    status?: number;
    statusCode?: number;
    code?: number;
    response?: { status?: number };
  };
  return (
    (value.status ??
      value.statusCode ??
      value.response?.status ??
      value.code) === 404
  );
}

function ownedStream(
  target: object,
  start: (...args: unknown[]) => { close(): void; wait?(): Promise<void> },
  config: Config,
  args: unknown[],
  onStartError?: (error: Error) => void,
): { close(): void; wait(): Promise<void> } {
  let closed = false;
  let handle: { close(): void; wait?(): Promise<void> } | undefined;
  const context = new Proxy(target, {
    get(object, property) {
      const value = Reflect.get(object, property, object);
      if (property === "withClient" && typeof value === "function") {
        return (...options: unknown[]) => {
          const request = Reflect.apply(value, object, options) as {
            client: object;
          };
          const client = new Proxy(request.client, {
            get(client, method) {
              const action = Reflect.get(client, method, client);
              return typeof action === "function"
                ? (...parameters: unknown[]) =>
                    configured(config, async () =>
                      Reflect.apply(action, client, parameters),
                    )
                : action;
            },
          });
          Object.defineProperty(request, "client", {
            value: client,
            configurable: true,
          });
          return request;
        };
      }
      if (typeof value === "function") {
        if (value.constructor.name === "AsyncFunction") {
          return (...parameters: unknown[]) =>
            configured(config, async () =>
              Reflect.apply(value, object, parameters),
            );
        }
        return value.bind(object);
      }
      return value;
    },
  });
  // Own request setup, not the lifetime of an open response stream.
  const started = configured(config, async () => {
    if (!closed) handle = Reflect.apply(start, context, args);
  });
  const done = started.catch((error: unknown) => {
    onStartError?.(error instanceof Error ? error : new Error(String(error)));
    throw error;
  }).then(() => handle?.wait?.());
  void done.catch(() => {});
  return {
    close() {
      closed = true;
      handle?.close();
    },
    wait: () => done,
  };
}

function owned(native: SandboxInstance, config: Config): SandboxInstance {
  const actions = new Set([
    "fs",
    "process",
    "network",
    "previews",
    "schedules",
    "sessions",
    "snapshots",
    "system",
    "drives",
    "codegen",
  ]);
  const proxies = new WeakMap<object, object>();
  function wrap<T extends object>(target: T): T {
    const existing = proxies.get(target);
    if (existing) return existing as T;
    const proxy = new Proxy(target, {
      get(object, property) {
        const value = Reflect.get(object, property, object);
        if (typeof value === "function") {
          if (property === "watch" || property === "streamLogs") {
            return (...args: unknown[]) =>
              ownedStream(
                object,
                value as (...args: unknown[]) => { close(): void; wait?(): Promise<void> },
                config,
                args,
                (args[property === "watch" ? 2 : 1] as { onError?: (error: Error) => void } | undefined)?.onError,
              );
          }
          if (value.constructor.name === "AsyncFunction") {
            return (...args: unknown[]) =>
              configured(config, () => Reflect.apply(value, object, args));
          }
          return value.bind(object);
        }
        return actions.has(String(property)) &&
          value &&
          typeof value === "object"
          ? wrap(value)
          : value;
      },
    });
    proxies.set(target, proxy);
    return proxy;
  }
  return stamp.stamp(wrap(native), config);
}

const provider = defineProvider<SandboxInstance, Config>({
  name: "blaxel",
  methods: {
    instances: {
      create: (config, options) =>
        configured(config, async () => {
          const {
            timeout,
            envs,
            creationTimeout,
            mountDrives,
            createIfNotExist = false,
            safe,
            runtime,
            ...rest
          } = options ?? {};
          if (runtime !== undefined)
            throw new TypeError(
              "Use a Blaxel image rather than the legacy runtime alias",
            );
          const instance = await SandboxInstance.create(
            {
              ...rest,
              ...(envs !== undefined
                ? {
                    envs: Object.entries(envs).map(([name, value]) => ({
                      name,
                      value,
                    })),
                  }
                : {}),
              ...(timeout !== undefined ? { ttl: `${timeout / 1000}s` } : {}),
            },
            {
              createIfNotExist,
              ...(mountDrives !== undefined ? {mountDrives: mountDrives as NonNullable<Parameters<typeof SandboxInstance.create>[1]>["mountDrives"]} : {}),
              ...(safe !== undefined ? { safe } : {}),
              ...(creationTimeout !== undefined
                ? { timeout: creationTimeout }
                : {}),
            },
          );
          const id = instance.metadata?.name;
          if (!id) throw new Error("Blaxel returned a sandbox without a name");
          return { instance: owned(instance, config), instanceId: id };
        }),
      getById: (config, id) =>
        configured(config, async () => {
          return onMissing(async () => {
            const instance = await SandboxInstance.get(id);
            return { instance: owned(instance, config), instanceId: id };
          }, missing, null);
        }),
      destroy: (config, id) =>
        configured(config, async () => {
          return onMissing(async () => {
            await SandboxInstance.delete(id);
          }, missing, undefined);
        }),
      list: (config) =>
        configured(config, async () => {
          const listed = await SandboxInstance.list();
          const instances: SandboxInstance[] = [];
          for await (const instance of listed) instances.push(instance);
          return instances.map((instance) => ({
            instance: owned(instance, config),
            instanceId: instance.metadata.name!,
          }));
        }),
      async runCommand(instance, command, options) {
        const start = Date.now();
        const result = await instance.process.exec({
          command,
          waitForCompletion: !options?.background,
          ...(options?.cwd !== undefined ? { workingDir: options.cwd } : {}),
          ...(options?.env !== undefined ? { env: options.env } : {}),
          ...(options?.timeout !== undefined
            ? { timeout: options.timeout / 1000 }
            : {}),
          ...(options?.onStdout ? { onStdout: options.onStdout } : {}),
          ...(options?.onStderr ? { onStderr: options.onStderr } : {}),
        });
        return {
          stdout: result.stdout ?? "",
          stderr: result.stderr ?? "",
          exitCode: result.exitCode ?? 0,
          durationMs: Date.now() - start,
        };
      },
      async getInfo(instance) {
        const created = instance.metadata?.createdAt;
        if (!created)
          throw new Error("Blaxel did not return a creation timestamp");
        return {
          id: instance.metadata.name!,
          provider: "blaxel",
          status:
            instance.status?.toLowerCase() === "failed" ? "error" : "running",
          createdAt: new Date(created),
          timeout: 0,
          metadata: instance.metadata.labels,
        };
      },
      async getUrl() {
        throw new Error(
          "Use native Blaxel previews or fetch; generic public URL creation is not implemented",
        );
      },
      filesystem: {
        readFile: (instance, path) => instance.fs.read(path),
        writeFile: async (instance, path, content) => {
          await instance.fs.write(path, content);
        },
        mkdir: async (instance, path) => {
          await instance.fs.mkdir(path);
        },
        remove: async (instance, path) => {
          await instance.fs.rm(path, true);
        },
        exists: async (instance, path) => {
          return onMissing(async () => {
            await instance.fs.read(path);
            return true;
          }, missing, false);
        },
        readdir: async (instance, path) => {
          const directory = await instance.fs.ls(path);
          return [
            ...directory.files.map((entry) => ({
              name: entry.name,
              type: "file" as const,
              size: entry.size,
              modified: new Date(entry.lastModified),
            })),
            ...directory.subdirectories.map((entry) => ({
              name: entry.name,
              type: "directory" as const,
            })),
          ];
        },
      },
      getInstance: (instance) => instance,
    },
  },
});

export function createNativeBlaxelProvider(config: Config = {}) {
  const native = settings.config;
  const apiKey =
    config.apiKey ??
    native.apiKey ??
    (native.apikey || undefined) ??
    (native.clientCredentials === undefined
      ? process.env.BL_API_KEY
      : undefined);
  return provider({
    apiKey,
    workspace:
      config.workspace ?? (native.workspace || process.env.BL_WORKSPACE),
    ...(apiKey === undefined && native.clientCredentials !== undefined
      ? { clientCredentials: native.clientCredentials }
      : {}),
  });
}
