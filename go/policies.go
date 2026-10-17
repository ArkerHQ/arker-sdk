package arker

import (
	"context"
	"encoding/json"
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
	// VMState is inbound only: "running" or "idle". Pair "idle" with "deny" to
	// keep requests from starting the VM.
	VMState string `json:"vm_state,omitempty"`
}

// PolicyActionObject is the object form of a rule's action: the request is
// forwarded (applying Rewrite), held until Gate allows it, and with Suspend
// the VM is suspended until the response arrives. Set at least one.
type PolicyActionObject struct {
	Rewrite *PolicyRewrite `json:"rewrite,omitempty"`
	Gate    *PolicyGate    `json:"gate,omitempty"`
	Suspend bool           `json:"suspend,omitempty"`
	// Deprecated: use Suspend. Not with Suspend.
	Scaling *PolicyScaling `json:"scaling,omitempty"`
}

// PolicyRewrite edits a matched outbound request before it is forwarded.
type PolicyRewrite struct {
	Host              string            `json:"host,omitempty"`
	Path              string            `json:"path,omitempty"`
	Headers           map[string]string `json:"headers,omitempty"`
	RemoveHeaders     []string          `json:"remove_headers,omitempty"`
	RemoveQueryParams []string          `json:"remove_query_params,omitempty"`
	Body              string            `json:"body,omitempty"`
}

// PolicyGate holds a matched request until an HTTP check answers with one of
// AllowOnStatus.
type PolicyGate struct {
	Host          string            `json:"host"`
	AllowOnStatus []int             `json:"allow_on_status"`
	Path          string            `json:"path,omitempty"`
	Method        string            `json:"method,omitempty"`
	Headers       map[string]string `json:"headers,omitempty"`
	Body          any               `json:"body,omitempty"`
	DenyOnTimeout *bool             `json:"deny_on_timeout,omitempty"`
	TimeoutMS     *int              `json:"timeout_ms,omitempty"`
}

// PolicyScaling is the deprecated spelling of PolicyActionObject.Suspend.
type PolicyScaling struct {
	Suspend bool `json:"suspend"`
}

// PolicyEntry is one rule of the ordered (deprecated) policy list. Type is
// "outbound" or "inbound". Action is "allow", "deny", or "suspend" (suspend the
// VM until a matched outbound response arrives); set ActionObject instead for
// the object form.
type PolicyEntry struct {
	Type         string              `json:"type"`
	Match        *PolicyMatch        `json:"match,omitempty"`
	Action       string              `json:"-"`
	ActionObject *PolicyActionObject `json:"-"`
	Auth         string              `json:"auth,omitempty"`
}

// PolicyRule is one rule of a VM's keyed policy. Type "outbound" and
// "inbound" rules take Priority (required; higher is evaluated first), Match
// and ExpiresAt. Type "schedule" rules name exactly one clock -- At,
// AfterCreated or AfterLastRunningStart (seconds) -- and Action "suspend" or
// "delete".
type PolicyRule struct {
	Type                  string              `json:"type"`
	Priority              *int                `json:"priority,omitempty"`
	Match                 *PolicyMatch        `json:"match,omitempty"`
	Action                string              `json:"-"`
	ActionObject          *PolicyActionObject `json:"-"`
	ExpiresAt             string              `json:"expires_at,omitempty"`
	At                    string              `json:"at,omitempty"`
	AfterCreated          int                 `json:"after_created,omitempty"`
	AfterLastRunningStart int                 `json:"after_last_running_start,omitempty"`
}

// The wire "action" is a string or an object; these carry it in and out of
// the Action / ActionObject pair.

func (e PolicyEntry) MarshalJSON() ([]byte, error) {
	type plain PolicyEntry
	return json.Marshal(struct {
		plain
		Action any `json:"action"`
	}{plain(e), wireAction(e.Action, e.ActionObject)})
}

func (e *PolicyEntry) UnmarshalJSON(raw []byte) error {
	type plain PolicyEntry
	wire := struct {
		*plain
		Action json.RawMessage `json:"action"`
	}{plain: (*plain)(e)}
	if err := json.Unmarshal(raw, &wire); err != nil {
		return err
	}
	return readAction(wire.Action, &e.Action, &e.ActionObject)
}

func (r PolicyRule) MarshalJSON() ([]byte, error) {
	type plain PolicyRule
	return json.Marshal(struct {
		plain
		Action any `json:"action"`
	}{plain(r), wireAction(r.Action, r.ActionObject)})
}

func (r *PolicyRule) UnmarshalJSON(raw []byte) error {
	type plain PolicyRule
	wire := struct {
		*plain
		Action json.RawMessage `json:"action"`
	}{plain: (*plain)(r)}
	if err := json.Unmarshal(raw, &wire); err != nil {
		return err
	}
	return readAction(wire.Action, &r.Action, &r.ActionObject)
}

func wireAction(name string, object *PolicyActionObject) any {
	if object != nil {
		return object
	}
	return name
}

func readAction(raw json.RawMessage, name *string, object **PolicyActionObject) error {
	if len(raw) == 0 {
		return nil
	}
	if raw[0] == '{' {
		*object = new(PolicyActionObject)
		return json.Unmarshal(raw, *object)
	}
	return json.Unmarshal(raw, name)
}

// PolicyDoc is a VM's network policy. Rules is the keyed policy a VM reports
// and PATCH /v1/vms/{id} (UpdateRequest.Policies) changes: a rule replaces the
// one with its id, a nil rule removes it, and rules not named are kept.
// Policies is the deprecated ordered list, which replaces the whole policy and
// is all the PUT /policies document takes. Hostname and Warnings are
// response-only.
type PolicyDoc struct {
	Rules       map[string]*PolicyRule `json:"rules,omitempty"`
	Policies    []PolicyEntry          `json:"policies,omitempty"`
	Secrets     map[string]any         `json:"secrets,omitempty"`
	MITMDomains []string               `json:"mitm_domains,omitempty"`
	Hostname    string                 `json:"hostname,omitempty"`
	Warnings    []string               `json:"warnings,omitempty"`
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
