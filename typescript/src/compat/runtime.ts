import type { RequestTransportOptions } from "../index.js";
import { compute } from "computesdk";

import { createArkerComputeProvider, type ArkerComputeProviderConfig, type ForkMetadataMapper, type ForkExecutor } from "./arker-provider.js";
import { createRoutedProvider } from "./router.js";

type ComputeFactory = (config: {
  providers: unknown[];
  fallbackOnError: boolean;
}) => ReturnType<typeof compute>;

let computeFactory: ComputeFactory = compute as ComputeFactory;

export type CompatSdk = ReturnType<typeof compute>;

export interface CreateCompatSdkOptions {
  arker?: ArkerComputeProviderConfig;
  arkerLookupTransport?: RequestTransportOptions;
  fallbackProvider: unknown;
  routerConfigPath?: string;
  mapArkerForkMetadata?: ForkMetadataMapper;
  arkerFork?: ForkExecutor;
  arkerLifetimePolicyId?: string;
  mapArkerCreateOptions?: (options: Record<string, unknown>) => Record<string, unknown>;
  prepareArkerCreateOptions?: (options: Record<string, unknown>) => Promise<{ options: Record<string, unknown>; context: unknown }>;
  finishArkerCreate?: (sandbox: any, context: unknown) => Promise<void>;
}

export function createCompatRouter(options: CreateCompatSdkOptions) {
  const hasArker = !!(options.arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
  const arker = hasArker ? createArkerComputeProvider(options.arker, options.mapArkerForkMetadata, options.arkerLookupTransport, options.arkerFork, options.arkerLifetimePolicyId) : undefined;
  if (arker && (options.mapArkerCreateOptions || options.prepareArkerCreateOptions)) {
    const create = arker.sandbox.create.bind(arker.sandbox);
    arker.sandbox.create = async (values) => {
      if (options.prepareArkerCreateOptions) {
        const prepared = await options.prepareArkerCreateOptions(values ?? {});
        const sandbox = await create(prepared.options);
        await options.finishArkerCreate?.(sandbox, prepared.context);
        return sandbox;
      }
      return create(options.mapArkerCreateOptions!(values ?? {}));
    };
  }
  return createRoutedProvider([
    ...(arker ? [arker] : []),
    options.fallbackProvider as any,
  ], options.routerConfigPath ?? globalThis.process?.env?.COMPUTESDK_ROUTER_CONFIG);
}

export function createCompatSdk(options: CreateCompatSdkOptions): CompatSdk {
  return computeFactory({ providers: [createCompatRouter(options)], fallbackOnError: false });
}

export function setCompatComputeFactoryForTests(factory?: ComputeFactory): void {
  computeFactory = factory ?? (compute as ComputeFactory);
}
