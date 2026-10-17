import { Sandbox, NotFoundError, CommandExitError, FileType } from "e2b";
import { defineProvider } from "@computesdk/provider";

const provider = defineProvider<Sandbox, Record<string, never>>({
  name: "e2b",
  methods: {
    instances: {
      async create(_config, options) {
        const { templateId, snapshotId, timeout, ...rest } = options ?? {};
        const template = templateId ?? snapshotId;
        const nativeOptions = {
          ...rest,
          ...(timeout !== undefined ? { timeoutMs: timeout } : {}),
        };
        const instance =
          template !== undefined
            ? await Sandbox.create(template, nativeOptions)
            : await Sandbox.create(nativeOptions);
        return { instance, instanceId: instance.sandboxId };
      },
      async getById(_config, id) {
        try {
          return { instance: await Sandbox.connect(id), instanceId: id };
        } catch (error) {
          if (error instanceof NotFoundError) return null;
          throw error;
        }
      },
      async destroy(_config, id) {
        await Sandbox.kill(id);
      },
      async list() {
        throw new Error(
          "Use the native Sandbox.list paginator; generic E2B listing is not implemented",
        );
      },
      async runCommand(instance, command, options) {
        const start = Date.now();
        const { env, timeout, ...rest } = options ?? {};
        try {
          const result = await instance.commands.run(command, {
            ...rest,
            ...(env !== undefined ? { envs: env } : {}),
            ...(timeout !== undefined ? { timeoutMs: timeout } : {}),
          });
          return {
            stdout: result.stdout,
            stderr: result.stderr,
            exitCode: result.exitCode ?? 0,
            durationMs: Date.now() - start,
          };
        } catch (error) {
          if (!(error instanceof CommandExitError)) throw error;
          return {
            stdout: error.stdout,
            stderr: error.stderr,
            exitCode: error.exitCode,
            durationMs: Date.now() - start,
          };
        }
      },
      async getInfo(instance) {
        const info = await instance.getInfo();
        return {
          id: info.sandboxId,
          provider: "e2b",
          status: info.state === "running" ? "running" : "stopped",
          createdAt: info.startedAt,
          timeout: info.endAt.getTime() - info.startedAt.getTime(),
          metadata: info.metadata,
        };
      },
      async getUrl(instance, { port, protocol = "https" }) {
        return `${protocol}://${instance.getHost(port)}`;
      },
      filesystem: {
        readFile: (instance, path) => instance.files.read(path),
        writeFile: async (instance, path, content) => {
          await instance.files.write(path, content);
        },
        mkdir: async (instance, path) => {
          await instance.files.makeDir(path);
        },
        exists: (instance, path) => instance.files.exists(path),
        remove: async (instance, path) => {
          await instance.files.remove(path);
        },
        readdir: async (instance, path) =>
          (await instance.files.list(path)).map((entry) => ({
            name: entry.name,
            type: entry.type === FileType.DIR ? "directory" : "file",
            size: entry.size,
            modified: entry.modifiedTime,
          })),
      },
      getInstance: (instance) => instance,
    },
  },
});

export function createNativeE2BProvider() {
  return Object.assign(provider({}), {
    ownershipProbe: async (id: string) => {
      try {
        await Sandbox.getInfo(id);
        return true;
      } catch (error) {
        if (error instanceof NotFoundError) return false;
        throw error;
      }
    },
  });
}
