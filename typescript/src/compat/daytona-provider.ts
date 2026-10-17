import { Daytona, type DaytonaConfig, type Sandbox } from "@daytona/sdk";
import { defineProvider } from "@computesdk/provider";
import { randomUUID } from "node:crypto";
import { shellQuote } from "./common.js";

function missing(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as {
    status?: number;
    statusCode?: number;
    response?: { status?: number };
  };
  return (value.statusCode ?? value.status ?? value.response?.status) === 404;
}

export function createNativeDaytonaProvider(config: DaytonaConfig = {}) {
  let native: Daytona | undefined;
  const client = () => (native ??= new Daytona(config));
  const provider = defineProvider<Sandbox, Record<string, never>>({
    name: "daytona",
    methods: {
      instances: {
        async create(_config, options) {
          const { envs, snapshotId, templateId, runtime, timeout, ...rest } =
            options ?? {};
          const instance = await client().create(
            {
              ...rest,
              ...(envs !== undefined ? { envVars: envs } : {}),
              ...(runtime !== undefined ? { language: runtime } : {}),
              ...(templateId !== undefined || snapshotId !== undefined
                ? { snapshot: templateId ?? snapshotId }
                : {}),
            } as Parameters<Daytona["create"]>[0],
            timeout !== undefined ? { timeout: timeout / 1000 } : undefined,
          );
          return { instance, instanceId: instance.id };
        },
        async getById(_config, id) {
          try {
            const instance = await client().get(id);
            return { instance, instanceId: instance.id };
          } catch (error) {
            if (missing(error)) return null;
            throw error;
          }
        },
        async list() {
          const items = [];
          for await (const instance of client().list())
            items.push({ instance, instanceId: instance.id });
          return items;
        },
        async destroy(_config, id) {
          try {
            await (await client().get(id)).delete();
          } catch (error) {
            if (!missing(error)) throw error;
          }
        },
        async runCommand(instance, command, options) {
          const start = Date.now();
          const file = `/tmp/.arker-daytona-stderr-${randomUUID()}`;
          const marker = `arker-daytona-exit-${randomUUID()}:`;
          const actual = options?.background
            ? `nohup /bin/sh -c ${shellQuote(command)} >/dev/null 2>&1 &`
            : command;
          const wrapped = `( ${actual}\n) 2>${shellQuote(file)}; __arker_exit=$?; printf '\\n${marker}%s\\n' "$__arker_exit"; cat ${shellQuote(file)}; rm -f ${shellQuote(file)}`;
          const result = await instance.process.executeCommand(
            wrapped,
            options?.cwd,
            options?.env,
            options?.timeout !== undefined
              ? Math.ceil(options.timeout / 1000)
              : undefined,
          );
          const boundary = result.result.lastIndexOf("\n" + marker);
          if (boundary < 0)
            throw new Error(
              "Daytona command ended without a complete exit-status envelope",
            );
          const output = result.result.slice(boundary + marker.length + 1);
          const end = output.indexOf("\n");
          const exitCode = Number(output.slice(0, end));
          if (end < 0 || !Number.isInteger(exitCode))
            throw new Error("Invalid Daytona command exit-status envelope");
          return {
            stdout: result.result.slice(0, boundary),
            stderr: output.slice(end + 1),
            exitCode,
            durationMs: Date.now() - start,
          };
        },
        async getInfo(instance) {
          if (!instance.createdAt)
            throw new Error(
              "Daytona sandbox metadata does not include its creation time; refresh native data first",
            );
          return {
            id: instance.id,
            provider: "daytona",
            status: instance.state === "started" ? "running" : "stopped",
            createdAt: new Date(instance.createdAt),
            timeout: 0,
            metadata: instance.labels,
          };
        },
        async getUrl(instance, { port, protocol = "https" }) {
          const { url } = await instance.getPreviewLink(port);
          const parsed = new URL(url);
          parsed.protocol = protocol + ":";
          return parsed.toString();
        },
        filesystem: {
          readFile: async (instance, path) =>
            (await instance.fs.downloadFile(path)).toString("utf8"),
          writeFile: (instance, path, content) =>
            instance.fs.uploadFile(Buffer.from(content), path),
          mkdir: (instance, path) => instance.fs.createFolder(path, "755"),
          remove: (instance, path) => instance.fs.deleteFile(path, true),
          exists: async (instance, path) => {
            try {
              await instance.fs.getFileDetails(path);
              return true;
            } catch (error) {
              if (missing(error)) return false;
              throw error;
            }
          },
          readdir: async (instance, path) =>
            (await instance.fs.listFiles(path)).map((entry) => ({
              name: entry.name,
              type: entry.isDir ? "directory" : "file",
              size: entry.size,
              modified: new Date(entry.modTime),
            })),
        },
        getInstance: (instance) => instance,
      },
    },
  })({});
  return Object.assign(provider, {
    close: async () => {
      await native?.[Symbol.asyncDispose]();
    },
  });
}
