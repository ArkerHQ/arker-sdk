package arker

import (
	"context"
	"net/http"
)

// Scope limits what an API key can reach. Build them with ScopeAll, ScopeVM,
// ScopeOp and ScopePoolJoin.
type Scope string

// Op is an operation a key can be limited to.
type Op string

const (
	OpSync Op = "sync"
	OpPTY  Op = "pty"
	OpRun  Op = "run"
)

// ScopeAll reaches everything the key's role allows. It is the default.
const ScopeAll Scope = "*"

// ScopeVM limits a key to one VM.
func ScopeVM(vmID string) Scope { return Scope("vmid:" + vmID) }

// ScopeOp limits a key to one operation.
func ScopeOp(op Op) Scope { return Scope("op:" + string(op)) }

// ScopePoolJoin makes a key a worker join key for one pool. It must be the
// key's only scope.
func ScopePoolJoin(poolID string) Scope { return Scope("pool:" + poolID + ":join") }

// APIKey is an API key's metadata. The secret is only in CreatedAPIKey.
type APIKey struct {
	APIKeyID string  `json:"api_key_id"`
	Name     *string `json:"name"`
	// Role is "viewer", "member", "developer" or "admin".
	Role      string  `json:"role"`
	Scopes    []Scope `json:"scopes"`
	CreatedAt string  `json:"created_at"`
	ExpiresAt *string `json:"expires_at"`
}

// CreatedAPIKey is a new key. Key is the secret and is shown only once.
type CreatedAPIKey struct {
	APIKey APIKey `json:"api_key"`
	Key    string `json:"key"`
}

// CreateAPIKeyRequest describes a new key. The key's role cannot exceed the
// caller's and its reach is limited to the caller's.
type CreateAPIKeyRequest struct {
	Name string `json:"name,omitempty"`
	// Role defaults to the caller's role.
	Role string `json:"role,omitempty"`
	// Scopes defaults to ScopeAll.
	Scopes []Scope `json:"scopes,omitempty"`
	// ExpiresAt (RFC 3339) defaults to never.
	ExpiresAt string `json:"expires_at,omitempty"`
}

// ListAPIKeys lists the organization's API keys.
func (c *Client) ListAPIKeys(ctx context.Context) ([]APIKey, error) {
	var out struct {
		APIKeys []APIKey `json:"api_keys"`
	}
	_, err := c.control(ctx, "/v1/api-keys", &out)
	return out.APIKeys, err
}

// CreateAPIKey mints a key.
func (c *Client) CreateAPIKey(ctx context.Context, req CreateAPIKeyRequest) (*CreatedAPIKey, error) {
	var out CreatedAPIKey
	_, err := c.do(ctx, call{method: http.MethodPost, path: "/v1/api-keys", base: c.controlURL, body: req, out: &out})
	return &out, err
}

// RevokeAPIKey revokes a key.
func (c *Client) RevokeAPIKey(ctx context.Context, apiKeyID string) error {
	_, err := c.do(ctx, call{method: http.MethodDelete, path: "/v1/api-keys/" + segment(apiKeyID), base: c.controlURL})
	return err
}
