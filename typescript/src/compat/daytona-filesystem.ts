import { randomUUID } from "node:crypto";
import { open } from "node:fs/promises";
import { constants } from "node:fs";
import { DaytonaError, DaytonaBadRequestError, DaytonaForbiddenError, DaytonaNotFoundError, type FileInfo, type FilePermissionsParams, type FileUpload, type FileDownloadRequest, type FileDownloadResponse } from "@daytona/sdk";
import { ArkerError, type VM } from "../index.js";
import { assertSupportedObjectKeys, guardUnsupported, shellQuote, type UniversalSandbox } from "./common.js";
import source from "./daytona-filesystem.generated.js";

function filesystemError(message: string, status: number): DaytonaError {
  const ErrorType = status === 404 ? DaytonaNotFoundError : status === 403 ? DaytonaForbiddenError : DaytonaBadRequestError;
  return new ErrorType(message, status);
}

function checkTimeout(timeout = 1800): void {
  if (timeout !== 1800) throw new ArkerError("unsupported_operation", "Custom Daytona filesystem timeouts are not supported on Arker", 422);
}

async function localFile(path: string, data?: Buffer): Promise<Buffer | undefined> {
  const writing = data !== undefined;
  const flags = constants.O_NONBLOCK | (writing ? constants.O_WRONLY | constants.O_CREAT : constants.O_RDONLY);
  try {
    const handle = await open(path, flags, 0o666);
    try {
      if (!(await handle.stat()).isFile()) throw new Error("local transfers require a regular file");
      if (writing) { await handle.truncate(0); await handle.writeFile(data); return; }
      return await handle.readFile();
    } finally { await handle.close(); }
  } catch (error) { throw new DaytonaError(error instanceof Error ? error.message : String(error)); }
}

export function daytonaGuestCommand(request: Record<string, unknown>): string {
  return `setpriv --reuid=daytona --regid="$(id -g daytona)" --init-groups -- python3 -I -c ${shellQuote(source)} ${shellQuote(JSON.stringify(request))}`;
}

export class DaytonaFileSystem {
  constructor(private readonly box: UniversalSandbox) {
    return guardUnsupported(this, "daytona filesystem");
  }

  private async run(command: string): Promise<string> {
    const result = await this.box.runCommand(command);
    if (result.exitCode !== 0) {
      let error: { message: string; status: number };
      try { error = JSON.parse(result.stderr); }
      catch { throw new DaytonaError(result.stderr || "Daytona filesystem command failed"); }
      throw filesystemError(error.message, error.status);
    }
    return result.stdout;
  }

  private command(operation: string, path: string, options: Record<string, unknown> = {}): string {
    if (typeof path !== "string" || !path || path.includes("\0")) throw filesystemError("path must be a nonempty string without NUL", 400);
    return daytonaGuestCommand({ operation, path, ...options });
  }

  private async call<T>(operation: string, path: string, options?: Record<string, unknown>): Promise<T> {
    return JSON.parse(await this.run(this.command(operation, path, options)));
  }

  private async transfer(operation: "upload" | "download", path: string, data?: Buffer): Promise<Buffer | undefined> {
    const command = this.command(operation, path);
    const vm = (this.box as unknown as { getInstance(): VM }).getInstance();
    const directory = `/tmp/.arker-daytona-fs-${randomUUID()}`;
    const staged = `${directory}/data`;
    await this.run(`mkdir -m 700 -- ${shellQuote(directory)}`);
    let failed = false;
    try {
      if (operation === "upload") {
        await this.run(`umask 077; set -C; : > ${shellQuote(staged)}`);
        await vm.sync(staged, data!);
        await this.run(`${command} < ${shellQuote(staged)}`);
        return;
      }
      await this.run(`umask 077; set -C; ${command} > ${shellQuote(staged)}`);
      return Buffer.from(await vm.sync(staged));
    } catch (error) { failed = true; throw error; }
    finally {
      try { await this.run(`rm -rf -- ${shellQuote(directory)}`); }
      catch (error) { if (!failed) throw error; }
    }
  }

  async createFolder(path: string, mode: string): Promise<void> { await this.call("mkdir", path, { mode }); }
  async deleteFile(path: string, recursive = false): Promise<void> { await this.call("remove", path, { recursive }); }
  async getFileDetails(path: string): Promise<FileInfo> { return this.call("info", path); }
  async listFiles(path: string, options?: { depth?: number }): Promise<FileInfo[]> {
    assertSupportedObjectKeys(options, ["depth"], "daytona", "fs.listFiles");
    if (options?.depth !== undefined && (!Number.isInteger(options.depth) || options.depth < 1)) throw new DaytonaError("depth must be at least 1");
    return this.call("list", path, { depth: options?.depth });
  }
  async moveFiles(source: string, destination: string): Promise<void> { await this.call("move", source, { destination }); }
  async setFilePermissions(path: string, permissions: FilePermissionsParams): Promise<void> {
    assertSupportedObjectKeys(permissions, ["mode", "owner", "group"], "daytona", "fs.setFilePermissions");
    await this.call("permissions", path, permissions);
  }
  async uploadFile(file: Buffer, remotePath: string, timeout?: number): Promise<void>;
  async uploadFile(localPath: string, remotePath: string, timeout?: number): Promise<void>;
  async uploadFile(src: Buffer | string, dst: string, timeout = 1800): Promise<void> {
    checkTimeout(timeout);
    if (typeof src !== "string" && !Buffer.isBuffer(src)) throw new TypeError("source must be Buffer or a local path");
    await this.transfer("upload", dst, typeof src === "string" ? (await localFile(src))! : src);
  }
  async uploadFiles(files: FileUpload[], timeout = 1800): Promise<void> {
    checkTimeout(timeout);
    for (const file of files) {
      if (typeof file.source === "string") await this.uploadFile(file.source, file.destination, timeout);
      else await this.uploadFile(file.source, file.destination, timeout);
    }
  }
  async downloadFile(remotePath: string, timeout?: number): Promise<Buffer>;
  async downloadFile(remotePath: string, localPath: string, timeout?: number): Promise<void>;
  async downloadFile(src: string, dst?: string | number, timeout = 1800): Promise<Buffer | void> {
    checkTimeout(typeof dst === "number" ? dst : timeout);
    const data = (await this.transfer("download", src))!;
    if (typeof dst === "string") { await localFile(dst, data); return; }
    return data;
  }
  async downloadFiles(files: FileDownloadRequest[], timeout = 1800): Promise<FileDownloadResponse[]> {
    checkTimeout(timeout);
    const results: FileDownloadResponse[] = [];
    for (const file of files) {
      try {
        const result = file.destination ? await this.downloadFile(file.source, file.destination, timeout) : await this.downloadFile(file.source, timeout);
        results.push({ source: file.source, result: file.destination || result || Buffer.alloc(0) });
      } catch (error) {
        if (!(error instanceof DaytonaError)) throw error;
        results.push({ source: file.source, error: error.message, errorDetails: {
          message: error.message, statusCode: error.statusCode, code: error.code, source: error.source,
        } });
      }
    }
    return results;
  }

}
