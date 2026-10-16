package arker

import (
	"context"
	"fmt"
	"net/http"
)

type PoolResources struct {
	VCPU      int64 `json:"vcpu"`
	MemoryMiB int64 `json:"memory_mib"`
	DiskMiB   int64 `json:"disk_mib"`
}

type PoolCommitment struct {
	StartsAt string `json:"starts_at"`
	EndsAt   string `json:"ends_at"`
}

type CreatePoolBYOC struct {
	AccountID        string   `json:"account_id"`
	SubnetIDs        []string `json:"subnet_ids"`
	SecurityGroupIDs []string `json:"security_group_ids,omitempty"`
	InstanceTypes    []string `json:"instance_types,omitempty"`
}

// PoolBYOC is where a byoc pool's workers run; this shape is for provider aws.
type PoolBYOC struct {
	AccountID        string   `json:"account_id"`
	SubnetIDs        []string `json:"subnet_ids"`
	SecurityGroupIDs []string `json:"security_group_ids"`
	InstanceTypes    []string `json:"instance_types"`
	RoleName         string   `json:"role_name"`
	ExternalID       string   `json:"external_id"`
	SetupURL         string   `json:"setup_url"`
}

// Pool.Type is managed, byoc, or self_hosted. Provider belongs to managed/byoc
// pools; Commitments to managed; Isolation and BYOC to byoc. A self_hosted
// pool's Platform is nil until its first worker joins, and its Resources is
// nil when uncapped.
type Pool struct {
	PoolID               string           `json:"pool_id"`
	Type                 string           `json:"type"`
	Name                 *string          `json:"name"`
	Region               string           `json:"region"`
	Provider             string           `json:"provider,omitempty"`
	Platform             *string          `json:"platform"`
	State                string           `json:"state"`
	Reason               *string          `json:"reason"`
	CreatedAt            string           `json:"created_at"`
	Resources            *PoolResources   `json:"resources,omitempty"`
	ResourcesReserved    PoolResources    `json:"resources_reserved"`
	ResourcesProvisioned PoolResources    `json:"resources_provisioned"`
	Commitments          []PoolCommitment `json:"commitments,omitempty"`
	Isolation            []string         `json:"isolation,omitempty"`
	BYOC                 *PoolBYOC        `json:"byoc,omitempty"`
}

type PoolList struct {
	Pools      []Pool  `json:"pools"`
	NextCursor *string `json:"next_cursor"`
}

type ListPoolsOptions struct {
	Cursor string
	Limit  int
}

// CreatePoolRequest creates in the client's region. managed takes Platform,
// Resources, and Commitments; byoc takes Provider, Platform, Resources,
// Isolation, and BYOC; self_hosted takes only Name and an optional Resources cap.
type CreatePoolRequest struct {
	Type        string           `json:"type"`
	Provider    string           `json:"provider,omitempty"`
	Platform    string           `json:"platform,omitempty"`
	Name        string           `json:"name,omitempty"`
	Resources   *PoolResources   `json:"resources,omitempty"`
	Commitments []PoolCommitment `json:"commitments,omitempty"`
	Isolation   []string         `json:"isolation,omitempty"`
	BYOC        *CreatePoolBYOC  `json:"byoc,omitempty"`
}

// UpdatePoolRequest changes supplied fields. RenamePool can clear a name.
// A pointer to an empty commitment slice clears all unstarted commitments.
type UpdatePoolRequest struct {
	Name        *string           `json:"name,omitempty"`
	Resources   *PoolResources    `json:"resources,omitempty"`
	Commitments *[]PoolCommitment `json:"commitments,omitempty"`
}

type PoolWorker struct {
	WorkerID             string        `json:"worker_id"`
	Status               string        `json:"status"`
	ResourcesProvisioned PoolResources `json:"resources_provisioned"`
	ResourcesReserved    PoolResources `json:"resources_reserved"`
	LastSeenAt           string        `json:"last_seen_at"`
}
type PoolWorkerList struct {
	Workers []PoolWorker `json:"workers"`
}

func (c *Client) ListPools(ctx context.Context, opts ListPoolsOptions) (*PoolList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	var out PoolList
	_, err := c.control(ctx, q.on("/v1/pools"), &out)
	return &out, err
}
func (c *Client) GetPool(ctx context.Context, poolID string) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/pools/" + segment(poolID), out: &out})
	return &out, err
}
func (c *Client) CreatePool(ctx context.Context, req CreatePoolRequest) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPost, path: "/v1/pools", body: req, out: &out})
	return &out, err
}
func (c *Client) UpdatePool(ctx context.Context, poolID string, req UpdatePoolRequest) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPatch, path: "/v1/pools/" + segment(poolID), body: req, out: &out})
	return &out, err
}
func (c *Client) RenamePool(ctx context.Context, poolID string, name *string) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPatch, path: "/v1/pools/" + segment(poolID), body: map[string]*string{"name": name}, out: &out})
	return &out, err
}

// DeletePool deletes every VM in the pool; an active commitment refuses deletion.
func (c *Client) DeletePool(ctx context.Context, poolID string) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodDelete, path: "/v1/pools/" + segment(poolID), out: &out})
	return &out, err
}

// AddPoolCommitment reads then replaces commitments. Serialize concurrent edits.
func (c *Client) AddPoolCommitment(ctx context.Context, poolID string, commitment PoolCommitment) (*Pool, error) {
	pool, err := c.GetPool(ctx, poolID)
	if err != nil {
		return nil, err
	}
	if pool.Type != "managed" {
		return nil, fmt.Errorf("arker: %s pools do not support commitments", pool.Type)
	}
	commitments := append(pool.Commitments, commitment)
	return c.UpdatePool(ctx, poolID, UpdatePoolRequest{Commitments: &commitments})
}
func (c *Client) ListPoolWorkers(ctx context.Context, poolID string) (*PoolWorkerList, error) {
	var out PoolWorkerList
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/pools/" + segment(poolID) + "/workers", out: &out})
	return &out, err
}
