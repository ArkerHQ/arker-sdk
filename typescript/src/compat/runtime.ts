import { compute } from "computesdk";

import { createArkerComputeProvider, type ArkerComputeProviderConfig } from "./arker-provider.js";
import { createRoutedProvider } from "./router.js";

type ComputeFactory = (config: {
  providers: unknown[];
}) => ReturnType<typeof compute>;

let computeFactory: ComputeFactory = compute as ComputeFactory;

export type CompatSdk = ReturnType<typeof compute>;

export interface CreateCompatSdkOptions {
  arker?: ArkerComputeProviderConfig;
  fallbackProvider: unknown;
  routerConfigPath?: string;
  mapArkerCreateOptions?: (options: Record<string, unknown>) => Record<string, unknown>;
}

export function createCompatSdk(options: CreateCompatSdkOptions): CompatSdk {
  const hasArker = !!(options.arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
  const arker = hasArker ? createArkerComputeProvider(options.arker) : undefined;
  if (arker && options.mapArkerCreateOptions) {
    const create = arker.sandbox.create.bind(arker.sandbox);
    arker.sandbox.create = (values) => create(options.mapArkerCreateOptions!(values ?? {}));
  }
  const routed = createRoutedProvider([
    ...(arker ? [arker] : []),
    options.fallbackProvider as any,
  ], options.routerConfigPath ?? globalThis.process?.env?.COMPUTESDK_ROUTER_CONFIG);
  return computeFactory({
    providers: [routed],
  });
}

export function setCompatComputeFactoryForTests(factory?: ComputeFactory): void {
  computeFactory = factory ?? (compute as ComputeFactory);
}
