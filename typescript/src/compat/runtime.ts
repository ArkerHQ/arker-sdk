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
}

export function createCompatSdk(options: CreateCompatSdkOptions): CompatSdk {
  const hasArker = !!(options.arker?.apiKey || globalThis.process?.env?.ARKER_API_KEY?.trim());
  const routed = createRoutedProvider([
    ...(hasArker ? [createArkerComputeProvider(options.arker)] : []),
    options.fallbackProvider as any,
  ], options.routerConfigPath);
  return computeFactory({
    providers: [routed],
  });
}

export function setCompatComputeFactoryForTests(factory?: ComputeFactory): void {
  computeFactory = factory ?? (compute as ComputeFactory);
}
