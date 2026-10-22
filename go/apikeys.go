package arker

import (
	"context"
	"net/http"
)

// APIKeyScope limits a key to one VM or to one pool; set exactly one field.
type APIKeyScope struct {
	VMID   string `json:"vm_id,omitempty"`
	PoolID string `json:"pool_id,omitempty"`
}

// APIKey is a key as listed. Scopes is what the key is limited to: VMs, or one pool.
type APIKey struct {
	APIKeyID  string        `json:"api_key_id"`
	Scopes    []APIKeyScope `json:"scopes"`
	CreatedAt string        `json:"created_at"`
}

// CreatedAPIKey is a new key with its secret, which is returned only once.
type CreatedAPIKey struct {
	APIKey
	Key string `json:"key"`
}

// CreateAPIKeyRequest creates a key with the caller's role. Scopes limits it to
// 1 to 16 VMs or to one pool. Only admins create a key limited to a pool.
type CreateAPIKeyRequest struct {
	Name   string        `json:"name,omitempty"`
	Scopes []APIKeyScope `json:"scopes,omitempty"`
}

type APIKeyList struct {
	APIKeys []APIKey `json:"api_keys"`
}

// CreateAPIKey creates a key through the control plane.
func (c *Client) CreateAPIKey(ctx context.Context, req CreateAPIKeyRequest) (*CreatedAPIKey, error) {
	var out CreatedAPIKey
	_, err := c.do(ctx, call{method: http.MethodPost, base: c.controlURL, path: "/v1/api-keys", body: req, out: &out})
	return &out, err
}

// ListAPIKeys lists the organization's unrevoked keys, newest first. A key
// limited to a pool sees that pool's keys only.
func (c *Client) ListAPIKeys(ctx context.Context) (*APIKeyList, error) {
	var out APIKeyList
	_, err := c.control(ctx, "/v1/api-keys", &out)
	return &out, err
}

// RevokeAPIKey revokes a key; requests with it are refused from then on.
func (c *Client) RevokeAPIKey(ctx context.Context, apiKeyID string) error {
	_, err := c.do(ctx, call{method: http.MethodDelete, base: c.controlURL, path: "/v1/api-keys/" + segment(apiKeyID)})
	return err
}
