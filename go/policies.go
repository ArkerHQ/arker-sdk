package arker

import (
	"context"
	"net/http"
)

// PolicyMatch selects the traffic a PolicyEntry applies to.
type PolicyMatch struct {
	Ports        []any          `json:"ports,omitempty"`
	IPs          []string       `json:"ips,omitempty"`
	Hosts        []string       `json:"hosts,omitempty"`
	Methods      []string       `json:"methods,omitempty"`
	Paths        []string       `json:"paths,omitempty"`
	Headers      map[string]any `json:"headers,omitempty"`
	BodyContains []string       `json:"body_contains,omitempty"`
}

// PolicyEntry is one rule. Type is "outbound" or "inbound"; Action is "allow"
// or "deny".
type PolicyEntry struct {
	Type   string       `json:"type"`
	Match  *PolicyMatch `json:"match,omitempty"`
	Action string       `json:"action"`
	Auth   string       `json:"auth,omitempty"`
}

// PolicyDoc is a VM's complete network policy. An empty doc means allow-all.
// Hostname and Warnings are response-only.
type PolicyDoc struct {
	Policies    []PolicyEntry  `json:"policies,omitempty"`
	Secrets     map[string]any `json:"secrets,omitempty"`
	MITMDomains []string       `json:"mitm_domains,omitempty"`
	Hostname    string         `json:"hostname,omitempty"`
	Warnings    []string       `json:"warnings,omitempty"`
}

// GetPolicies reads this VM's network policy.
func (v *VM) GetPolicies(ctx context.Context) (*PolicyDoc, error) {
	var doc PolicyDoc
	_, err := v.do(ctx, http.MethodGet, v.path("/policies"), nil, &doc)
	return &doc, err
}

// SetPolicies replaces this VM's network policy wholesale and returns what was
// stored. An empty doc clears the policy to allow-all.
func (v *VM) SetPolicies(ctx context.Context, doc PolicyDoc) (*PolicyDoc, error) {
	var out PolicyDoc
	_, err := v.do(ctx, http.MethodPut, v.path("/policies"), doc, &out)
	return &out, err
}
