import { createHash, createHmac } from "node:crypto";
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
type URLVM = Pick<VM, "id" | "baseUrl" | "_client">;
interface DelegationResponse {
  redemption_url: string;
  context: string;
  signing_key: string;
  operations: ("read" | "write")[];
  expires_at: number | null;
}
const delegations = new WeakMap<URLVM, FileDelegation>();
function decode(value: string, limit: number): Buffer {
  if (typeof value !== "string" || !value.length || value.length > limit || !/^[A-Za-z0-9_-]+$/.test(value)) throw new TypeError("Invalid file delegation encoding");
  const bytes = Buffer.from(value, "base64url");
  if (bytes.toString("base64url") !== value) throw new TypeError("Invalid file delegation encoding");
  return bytes;
}
class FileDelegation {
  readonly #key: Buffer;
  readonly #response: Omit<DelegationResponse, "signing_key">;
  constructor(response: DelegationResponse) {
    const url = new URL(response.redemption_url);
    if (url.protocol !== "https:" || url.username || url.password || /[?#\s\\]/.test(response.redemption_url)) throw new TypeError("Invalid file delegation redemption URL");
    decode(response.context, 4096);
    this.#key = decode(response.signing_key, 43);
    if (this.#key.length !== 32) throw new TypeError("Invalid file delegation key");
    if (!Array.isArray(response.operations) || !response.operations.length || response.operations.some(op => op !== "read" && op !== "write") || new Set(response.operations).size !== response.operations.length) throw new TypeError("Invalid file delegation operations");
    if (response.expires_at !== null && (!Number.isSafeInteger(response.expires_at) || response.expires_at < 0)) throw new TypeError("Invalid file delegation expiry");
    this.#response = { redemption_url: response.redemption_url, context: response.context, operations: [...response.operations], expires_at: response.expires_at };
  }
  sign(body: { operation: "read" | "write"; expires_at?: number }): string {
    const response = this.#response;
    if (!response.operations.includes(body.operation)) throw new Error("File operation is not delegated");
    if (response.expires_at !== null && (Math.floor(Date.now() / 1000) > response.expires_at || body.expires_at === undefined || body.expires_at > response.expires_at)) throw new Error("File URL expiry exceeds delegation lifetime");
    const payload = Buffer.from(JSON.stringify(body)).toString("base64url");
    const message = `${response.context}.${payload}`;
    const tag = createHmac("sha256", this.#key).update("arker:file-transfer:v1\0").update(message).digest("base64url");
    const ticket = `ark_file_v1_${message}.${tag}`;
    if (ticket.length > 16384) throw new Error("File capability is too large");
    return `${response.redemption_url}?ticket=${ticket}`;
  }
}
export async function prepareFileURLs(vm: URLVM): Promise<void> {
  const response = await vm._client._request<DelegationResponse>("POST", `/v1/vms/${encodeURIComponent(vm.id)}/file-transfer-delegation`, {}, vm.baseUrl);
  delegations.set(vm, new FileDelegation(response));
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
    if (typeof path !== "string" || path.includes("\0") || (!path && operation === "read") || Buffer.byteLength(path) > 4096) {
      throw new TypeError("path must be a string without NUL; downloads require a path");
    }
    if (user !== undefined && (typeof user !== "string" || user.includes(":") || user.includes("\0") || Buffer.byteLength(user) > 256)) {
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
    const delegation = delegations.get(vm);
    if (!delegation) throw new Error("File URLs require prepared signing authority; use create or connect");
    return delegation.sign({
      path, operation, envd_signature: signature,
      ...(user ? { user } : {}),
      ...(expiresAt === undefined ? {} : { expires_at: expiresAt }),
    } as { operation: "read" | "write"; expires_at?: number });
  }
}
