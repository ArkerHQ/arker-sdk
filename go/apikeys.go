package arker

import (
	"context"
	"net/http"
	"time"
)

// Scope limits what an API key can reach. A key reaches what its resource
// scopes and its operation scopes both allow:
//
//   - resource scopes: "*" (everything; the default, and only valid alone),
//     "vmid:<id>" (those VMs), "pool:<id>" (that pool's workers);
//   - operation scopes: "op:sync", "op:pty", "op:run", "op:heartbeat" limit
//     the key to those operations.
//
// A worker key is exactly WorkerKeyScopes(poolID), "pool:<id>" and
// "op:heartbeat"; neither scope is valid in any other set. A browser PTY key
// is PTYKeyScopes(vmID), "vmid:<id>" and "op:pty". Build the rest with
// ScopeAll, ScopeVM and ScopeOp.
type Scope string

// Op is an operation a key can be limited to. "heartbeat" is only part of
// WorkerKeyScopes.
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

// PTYKeyScopes are the scopes of a key that only opens terminals on one VM,
// which a browser passes as the PTY's api_key. See Client.CreatePTYKey.
func PTYKeyScopes(vmID string) []Scope { return []Scope{ScopeVM(vmID), ScopeOp(OpPTY)} }

// WorkerKeyScopes are the scopes of a worker key for one pool: exactly these
// two. Only an admin can create one.
func WorkerKeyScopes(poolID string) []Scope {
	return []Scope{Scope("pool:" + poolID), Scope("op:heartbeat")}
}

// ptyKeyTTL is how long CreatePTYKey keys live by default: long enough to open
// a WebSocket.
const ptyKeyTTL = 5 * time.Minute

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
// caller's, its reach is limited to the caller's, and it cannot outlive the
// caller: a later ExpiresAt is cut to the caller's.
type CreateAPIKeyRequest struct {
	Name string `json:"name,omitempty"`
	// Role defaults to the caller's role.
	Role string `json:"role,omitempty"`
	// Scopes defaults to ScopeAll.
	Scopes []Scope `json:"scopes,omitempty"`
	// ExpiresAt (RFC 3339) defaults to never.
	ExpiresAt string `json:"expires_at,omitempty"`
}

// ListAPIKeys lists the organization's unrevoked, unexpired API keys. It
// needs a caller with ScopeAll.
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

// CreatePTYKey mints a short-lived key that only opens terminals on one VM
// (PTYKeyScopes), for a browser to pass as api_key: a browser WebSocket cannot
// set an Authorization header. See VM.PTYURL. A ttl of zero means five
// minutes; the key also expires with the calling key if that is sooner.
func (c *Client) CreatePTYKey(ctx context.Context, vmID string, ttl time.Duration) (*CreatedAPIKey, error) {
	if ttl <= 0 {
		ttl = ptyKeyTTL
	}
	return c.CreateAPIKey(ctx, CreateAPIKeyRequest{
		Name:      "pty",
		Scopes:    PTYKeyScopes(vmID),
		ExpiresAt: time.Now().Add(ttl).UTC().Format(time.RFC3339),
	})
}

// RevokeAPIKey revokes a key no broader than the caller. It needs a caller
// with ScopeAll and a role above viewer.
func (c *Client) RevokeAPIKey(ctx context.Context, apiKeyID string) error {
	_, err := c.do(ctx, call{method: http.MethodDelete, path: "/v1/api-keys/" + segment(apiKeyID), base: c.controlURL})
	return err
}
