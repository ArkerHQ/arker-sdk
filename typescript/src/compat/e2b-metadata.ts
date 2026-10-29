import { manifest } from "./e2b-envd-guest.generated.js";
import type { ForkMetadataMapper } from "./arker-provider.js";

/** Separate caller metadata from the actual fork source; never inherit a parent envelope. */
export const e2bForkMetadata = (request: Parameters<ForkMetadataMapper>[0], metadata: unknown = {},
  network?: { allowPublicTraffic?: boolean }): ReturnType<ForkMetadataMapper> => {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata) ||
      Object.values(metadata).some(value => typeof value !== "string")) {
    throw new TypeError("E2B metadata must be a string-to-string object");
  }
  const source = request.source_vm_id
    ? { kind: "vm_id", value: request.source_vm_id }
    : request.source_vm_name ? { kind: "vm_name", value: request.source_vm_name } : undefined;
  if (!source) throw new TypeError("E2B metadata requires an actual VM source");
  return { arker_e2b: { version: 1, envd_version: manifest.version, metadata: { ...metadata }, source,
    ...(network === undefined ? {} : { network: { allow_public_traffic: network.allowPublicTraffic ?? true } }) } };
};
