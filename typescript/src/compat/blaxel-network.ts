import { ArkerError } from "../index.js";

/** Accept only native controls that leave existing VM policies unchanged. */
export function validateBlaxelNetwork(network: unknown): void {
  if (network === undefined || network === null) return;
  const fail = (message: string): never => { throw new ArkerError("unsupported_operation", message, 422); };
  const object = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
  if (!object(network)) return fail("Blaxel network must be a network configuration object");
  for (const key of Object.keys(network)) {
    if (key !== "firewall") fail(`Blaxel network.${key} requires native proxy or network services; Arker outbound policies are not equivalent`);
  }
  const firewall = network.firewall === undefined ? {} : network.firewall;
  if (!object(firewall) || Object.keys(firewall).some(key => key !== "rulesets"))
    return fail("Blaxel network.firewall supports only rulesets on Arker");
  const rulesets = firewall.rulesets === undefined ? [] : firewall.rulesets;
  if (!Array.isArray(rulesets) || rulesets.some(value => value !== "default"))
    fail("Blaxel network.firewall.rulesets supports only the no-op 'default' ruleset on Arker; proxy and dedicated-ip require native network services");
}
