import { ModalAppRecipe, buildNativeImage, nativeVolumes } from "./modal-recipes.js";
import { defineProvider } from "@computesdk/provider";
import {
  NotFoundError,
  SandboxFilesystemNotFoundError,
  type ModalClient,
  type Sandbox,
  type App,
  type Image,
  type SandboxCreateParams,
} from "modal";
import { onMissing, shellQuote } from "./common.js";

function missing(error: unknown): boolean {
  if (error instanceof NotFoundError) return true;
  const rpc = error as {
    code?: unknown;
    details?: unknown;
    path?: unknown;
  } | null;
  return (
    error instanceof Error &&
    rpc?.code === 5 &&
    typeof rpc.details === "string" &&
    typeof rpc.path === "string"
  );
}

export function createNativeModalProvider(client: () => ModalClient) {
  let app: Promise<App> | undefined;
  let image: Promise<Image> | undefined;
  const defaultApp = () =>
    (app ??= client()
      .apps.fromName("arker-compat", { createIfMissing: true })
      .catch((error) => {
        app = undefined;
        throw error;
      }));
  const defaultImage = (owner: App) =>
    (image ??= client()
      .images.fromRegistry("node:20")
      .build(owner)
      .catch((error) => {
        image = undefined;
        throw error;
      }));

  async function absolute(instance: Sandbox, path: string): Promise<string> {
    if (path.startsWith("/")) return path;
    const process = await instance.exec(["pwd"], {
      stdout: "pipe",
      stderr: "pipe",
    });
    const [directory, error, code] = await Promise.all([
      process.stdout.readText(),
      process.stderr.readText(),
      process.wait(),
    ]);
    if (code !== 0 || !directory.trim().startsWith("/"))
      throw new Error(`Cannot resolve Modal working directory: ${error}`);
    return directory.trim() + "/" + path;
  }

  return defineProvider<Sandbox, Record<string, never>>({
    name: "modal",
    methods: {
      instances: {
        async create(_config, options) {
          const {
            modalApp,
            modalImage,
            modalCreateParams,
            envs,
            timeout,
            ...rest
          } = (options ?? {}) as Record<string, any>;
          if ((modalApp === undefined) !== (modalImage === undefined))
            throw new TypeError(
              "Native Modal app and image must be supplied together",
            );
          const owner = modalApp instanceof ModalAppRecipe ? await modalApp.native(client()) : modalApp ?? (await defaultApp());
          const source = modalImage ? await buildNativeImage(modalImage, owner) : await defaultImage(owner);
          const params: SandboxCreateParams =
            modalCreateParams !== undefined
              ? modalCreateParams
              : {
                  ...rest,
                  ...(envs !== undefined ? { env: envs } : {}),
                  ...(timeout !== undefined ? { timeoutMs: timeout } : {}),
                };
          const instance = await client().sandboxes.create(
            owner,
            source,
            await nativeVolumes(client(), params),
          );
          return { instance, instanceId: instance.sandboxId };
        },
        async getById(_config, id) {
          return onMissing(async () => {
            const instance = await client().sandboxes.fromId(id);
            await instance.poll();
            return { instance, instanceId: instance.sandboxId };
          }, missing, null);
        },
        async list() {
          const values = [];
          for await (const instance of client().sandboxes.list())
            values.push({ instance, instanceId: instance.sandboxId });
          return values;
        },
        async destroy(_config, id) {
          return onMissing(async () => {
            await (await client().sandboxes.fromId(id)).terminate();
          }, missing, undefined);
        },
        async runCommand(instance, command, options) {
          const start = Date.now();
          const actual = options?.background
            ? `nohup /bin/sh -c ${shellQuote(command)} >/dev/null 2>&1 &`
            : command;
          const process = await instance.exec(["sh", "-c", actual], {
            stdout: "pipe",
            stderr: "pipe",
            workdir: options?.cwd,
            env: options?.env,
            timeoutMs: options?.timeout,
          });
          const [stdout, stderr, exitCode] = await Promise.all([
            process.stdout.readText(),
            process.stderr.readText(),
            process.wait(),
          ]);
          return { stdout, stderr, exitCode, durationMs: Date.now() - start };
        },
        async getInfo() {
          throw new Error(
            "Modal's native sandbox handle does not expose creation timestamps; generic metadata is not implemented",
          );
        },
        async getUrl(instance, { port, protocol }) {
          const tunnel = (await instance.tunnels())[port];
          if (!tunnel)
            throw new Error(
              `Modal port ${port} is not exposed by this sandbox`,
            );
          const url = new URL(tunnel.url);
          if (protocol) url.protocol = protocol + ":";
          return url.toString();
        },
        filesystem: {
          readFile: async (instance, path) =>
            instance.filesystem.readText(await absolute(instance, path)),
          writeFile: async (instance, path, content) => {
            await instance.filesystem.writeText(
              content,
              await absolute(instance, path),
            );
          },
          mkdir: async (instance, path) => {
            await instance.filesystem.makeDirectory(
              await absolute(instance, path),
              { createParents: true },
            );
          },
          readdir: async (instance, path) =>
            (
              await instance.filesystem.listFiles(
                await absolute(instance, path),
              )
            ).map((entry) => ({
              name: entry.name,
              type: entry.type === "directory" ? "directory" : "file",
              size: entry.size,
              modified: new Date(entry.modifiedTime * 1000),
            })),
          exists: async (instance, path) => {
            return onMissing(async () => {
              await instance.filesystem.stat(await absolute(instance, path));
              return true;
            }, (error) => error instanceof SandboxFilesystemNotFoundError, false);
          },
          remove: async (instance, path) => {
            if (
              path
                .split("/")
                .every((segment) => segment === "" || segment === ".")
            )
              throw new Error("Refusing ambiguous Modal removal path");
            return onMissing(async () => {
              await instance.filesystem.remove(await absolute(instance, path), {
                recursive: true,
              });
            }, (error) => error instanceof SandboxFilesystemNotFoundError, undefined);
          },
        },
        getInstance: (instance) => instance,
      },
    },
  })({});
}
