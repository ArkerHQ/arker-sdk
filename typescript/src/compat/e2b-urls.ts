import { createHash } from "node:crypto";
import type { EnvdDescriptor } from "./e2b-data-plane.js";
import type { VM } from "../index.js";

export interface E2BFileURLOptions {
  user?: string;
  useSignatureExpiration?: number;
}
interface NativeURLs {
  downloadUrl(path: string, options?: E2BFileURLOptions): Promise<string>;
  uploadUrl(path?: string, options?: E2BFileURLOptions): Promise<string>;
}
interface CapabilityResponse {
  vm_id: string;
  ticket: string;
  expires_at: number | null;
}

/** Revocable GET / multipart POST URLs for native file services.
 * Omitted expiry has no time limit; explicit expiry is 0..4294967295 seconds.
 * Revoking the issuing API key invalidates subsequent redemptions.
 */
export class E2BFileURLs {
  constructor(private readonly vm?: Pick<VM, "id" | "baseUrl" | "_client">, private readonly native?: NativeURLs, private readonly descriptor?: EnvdDescriptor) {}

  downloadUrl(path: string, options?: E2BFileURLOptions): Promise<string> {
    if (this.native) return this.native.downloadUrl(path, options);
    return this.mint("read", path, options);
  }

  uploadUrl(path?: string, options?: E2BFileURLOptions): Promise<string> {
    if (this.native) return this.native.uploadUrl(path, options);
    return this.mint("write", path, options);
  }

  private async mint(operation: "read" | "write", path: string | undefined, options: E2BFileURLOptions = {}): Promise<string> {
    const user = options.user;
    path = path ?? (operation === "write" ? "" : undefined);
    if (typeof path !== "string" || path.includes("\0") || (!path && operation === "read")) {
      throw new TypeError("path must be a string without NUL; downloads require a path");
    }
    if (user !== undefined && (typeof user !== "string" || user.includes(":") || user.includes("\0"))) {
      throw new TypeError("user must be an account name without colon or NUL");
    }
    const expiry = options.useSignatureExpiration;
    if (expiry !== undefined && (!Number.isInteger(expiry) || expiry < 0 || expiry > 4294967295)) {
      throw new RangeError("useSignatureExpiration must be an unsigned 32-bit integer in seconds");
    }
    if (!this.descriptor?.envd_access_token) throw new Error("File URLs require initialized sandbox file services");
    const expiresAt = expiry === undefined ? undefined : Math.floor(Date.now() / 1000) + expiry;
    let signed = `${path}:${operation}:${user ?? ""}:${this.descriptor.envd_access_token}`;
    if (expiresAt !== undefined) signed += `:${expiresAt}`;
    const signature = "v1_" + createHash("sha256").update(signed).digest("base64").replace(/=+$/, "");
    const vm = this.vm!;
    const base = new URL(vm.baseUrl);
    if (!["https:", "http:"].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
      throw new TypeError("Invalid file transfer API base URL");
    }
    const response = await vm._client._request<CapabilityResponse>(
      "POST", `/v1/vms/${encodeURIComponent(vm.id)}/file-transfer-urls`, {
        path, operation, envd_signature: signature,
        ...(user ? { user } : {}),
        ...(expiresAt === undefined ? {} : { expires_at: expiresAt }),
      }, vm.baseUrl,
    );
    if (typeof response.vm_id !== "string" || !response.vm_id || typeof response.ticket !== "string" || !response.ticket.startsWith("ark_file_v1_")) {
      throw new TypeError("Invalid file transfer capability response");
    }
    const url = new URL(`${vm.baseUrl.replace(/\/+$/, "")}/v1/vms/${encodeURIComponent(response.vm_id)}/file-transfer`);
    url.searchParams.set("ticket", response.ticket);
    return url.toString();
  }
}
