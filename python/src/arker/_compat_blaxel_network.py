"""Validate native network controls without weakening existing VM policies."""

from ._compat import UnsupportedCompatibilityOption


def validate_network(network):
    if network is None:
        return
    if hasattr(network, "to_dict"):
        network = network.to_dict()
    if not isinstance(network, dict):
        raise UnsupportedCompatibilityOption("Blaxel network must be a network configuration object")
    for key in network:
        if key != "firewall":
            raise UnsupportedCompatibilityOption(
                f"Blaxel network.{key} requires native proxy or network services; "
                "Arker outbound policies are not equivalent"
            )
    firewall = network.get("firewall", {})
    if hasattr(firewall, "to_dict"):
        firewall = firewall.to_dict()
    if not isinstance(firewall, dict) or set(firewall) - {"rulesets"}:
        raise UnsupportedCompatibilityOption("Blaxel network.firewall supports only rulesets on Arker")
    rulesets = firewall.get("rulesets", [])
    if not isinstance(rulesets, list) or any(value != "default" for value in rulesets):
        raise UnsupportedCompatibilityOption(
            "Blaxel network.firewall.rulesets supports only the no-op 'default' ruleset on Arker; "
            "proxy and dedicated-ip require native network services"
        )
