# E2B keyed creation and DNS

The E2B compatibility facades accept Python `idempotency_key` and TypeScript
`idempotencyKey`: 1–255 lowercase ASCII letters, digits or hyphens. Native routes
forward the original key. Arker routes map it to a domain-separated SHA-256 key
within Arker's 64-character limit and bind creation options to the fork request.
Changed creation payloads conflict instead of modifying an existing sandbox.

A keyed create uses one provider only. Weighted routing deterministically selects
that provider from the key and configured weights; priority routing selects the
first provider. Keyed requests never fall back to another provider, even if the
selected provider is unavailable. Keep routing configuration and credentials
unchanged across retries. There is no global ownership coordinator across
independently configured routers. Changing weights or provider credentials is
not a supported way to retry an existing key.

Arker retries also require a stable source and source policies. Private traffic
credentials are derived from the creator's credential and key, so credential
rotation can produce a conflict. Bootstrap failure or cancellation does not prove
that a keyed call created the returned sandbox: the facade closes local resources
but does not delete that sandbox. Retry the same key to recover it; an accepted
sandbox retains its configured lifetime deadline. Arker returns 404 after the
key's sandbox has been deleted; native E2B instead returns the original dead
sandbox descriptor. Neither recreates it.

`dns_servers` / `dnsServers` configures host firewall exceptions when an outbound
allowlist contains domains. It does not rewrite `/etc/resolv.conf`. Up to ten IPv4
addresses are accepted; omission defaults to `8.8.8.8`, and an empty list adds no
DNS exception. A separate IP/port guard prevents hostname guards from implicitly
opening other resolvers. Explicit IP/CIDR allow entries also retain port 53 access.
These exceptions permit all ports on the selected IPs, matching
native E2B's behavior.

Arker supports deny-all (`0.0.0.0/0`) with explicit IP, CIDR or domain exceptions.
Domain allowlists require deny-all. An empty or IP-only allowlist without deny-all
adds no restriction. DNS settings without domain selectors add no exception.
Selector callbacks receive native-shaped contexts with no registered transform
rules. Other deny selectors, transforms and proxy options are rejected before
creation. Host guards preserve inherited outbound restrictions; an exception
cannot broaden a source guard.

These mappings have unit coverage and native comparison evidence. Arker live
qualification of the combined keyed-creation/DNS adapter remains required.
