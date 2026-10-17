import { Sandbox, NotFoundError, CommandExitError, FileType } from "e2b";
import type {
  CreateSandboxOptions,
  RunCommandOptions,
  SandboxInterface,
  SandboxInfo,
  SandboxFileSystem,
  StartProcessOptions,
  ProcessHandle,
} from "computesdk";

export class NativeE2BSandbox implements SandboxInterface {
  readonly provider = "e2b";
  readonly instanceId: string;
  readonly sandboxId: string;
  readonly filesystem: SandboxFileSystem;

  constructor(private readonly native: Sandbox) {
    this.instanceId = this.sandboxId = native.sandboxId;
    this.filesystem = {
      readFile: (path) => native.files.read(path),
      writeFile: async (path, content) => {
        await native.files.write(path, content);
      },
      mkdir: async (path) => {
        await native.files.makeDir(path);
      },
      exists: (path) => native.files.exists(path),
      remove: async (path) => {
        await native.files.remove(path);
      },
      readdir: async (path) =>
        (await native.files.list(path)).map((entry) => ({
          name: entry.name,
          type: entry.type === FileType.DIR ? "directory" : "file",
          size: entry.size,
          modified: entry.modifiedTime,
        })),
    };
  }

  getInstance(): Sandbox {
    return this.native;
  }

  async runCommand(command: string, options?: RunCommandOptions) {
    const start = Date.now();
    const { env, timeout, ...rest } = options ?? {};
    try {
      const result = await this.native.commands.run(command, {
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
  }

  async startProcess(
    _command: string,
    _options?: StartProcessOptions,
  ): Promise<ProcessHandle> {
    throw new Error(
      "The E2B shim uses native commands.run({ background: true }) for process handles",
    );
  }

  async getInfo(): Promise<SandboxInfo> {
    const info = await this.native.getInfo();
    return {
      id: info.sandboxId,
      provider: this.provider,
      status: info.state === "running" ? "running" : "stopped",
      createdAt: info.startedAt,
      timeout: info.endAt.getTime() - info.startedAt.getTime(),
      metadata: info.metadata,
    };
  }

  async getUrl({
    port,
    protocol = "https",
  }: {
    port: number;
    protocol?: string;
  }): Promise<string> {
    return `${protocol}://${this.native.getHost(port)}`;
  }

  async destroy(): Promise<void> {
    await this.native.kill();
  }
}

export function createNativeE2BProvider() {
  return {
    name: "e2b",
    ownershipProbe: async (id: string) => {
      try {
        await Sandbox.getInfo(id);
        return true;
      } catch (error) {
        if (error instanceof NotFoundError) return false;
        throw error;
      }
    },
    instances: {
      async create(options?: CreateSandboxOptions): Promise<NativeE2BSandbox> {
        const { templateId, snapshotId, timeout, ...rest } = options ?? {};
        const template = templateId ?? snapshotId;
        const nativeOptions = {
          ...rest,
          ...(timeout !== undefined ? { timeoutMs: timeout } : {}),
        };
        return new NativeE2BSandbox(
          template !== undefined
            ? await Sandbox.create(template, nativeOptions)
            : await Sandbox.create(nativeOptions),
        );
      },
      async getById(id: string): Promise<NativeE2BSandbox | null> {
        try {
          return new NativeE2BSandbox(await Sandbox.connect(id));
        } catch (error) {
          if (error instanceof NotFoundError) return null;
          throw error;
        }
      },
      async destroy(id: string): Promise<void> {
        await Sandbox.kill(id);
      },
    },
  };
}
