package arker

import (
	"context"
	"net/http"
)

// A pool is created in the client's region; every other pool call goes through
// the control plane, which finds the pool's region. Organizations without pools
// get `not_found`.

// PoolResources is capacity in the same units as VM resources. Zero is a real
// size: resizing a pool to zero stops its charges.
type PoolResources struct {
	VCPU      int64 `json:"vcpu"`
	MemoryMiB int64 `json:"memory_mib"`
	DiskMiB   int64 `json:"disk_mib"`
}

// PoolCommitment keeps a pool's resources from StartsAt up to EndsAt, at least
// 24 hours, for a lower price. While one is active the pool cannot shrink or be
// deleted.
type PoolCommitment struct {
	StartsAt string `json:"starts_at"`
	EndsAt   string `json:"ends_at"`
}

// CreatePoolAWS is where a new `aws` pool's hosts run in the customer's AWS
// account.
type CreatePoolAWS struct {
	AccountID        string   `json:"account_id"`
	SubnetIDs        []string `json:"subnet_ids"`
	SecurityGroupIDs []string `json:"security_group_ids,omitempty"`
	InstanceTypes    []string `json:"instance_types,omitempty"`
}

// PoolAWS is an `aws` pool's placement and the role Arker assumes there: open
// SetupURL signed in to the account to create the role the pool waits for.
type PoolAWS struct {
	AccountID        string   `json:"account_id"`
	SubnetIDs        []string `json:"subnet_ids"`
	SecurityGroupIDs []string `json:"security_group_ids"`
	InstanceTypes    []string `json:"instance_types"`
	RoleName         string   `json:"role_name"`
	ExternalID       string   `json:"external_id"`
	SetupURL         string   `json:"setup_url"`
}

// Pool is capacity set aside for the organization's VMs in one region. Type is
// "arker", "aws" or "self_hosted"; the fields after CreatedAt are present only
// for the types the API documents them for.
type Pool struct {
	PoolID      string  `json:"pool_id"`
	Type        string  `json:"type"`
	Name        *string `json:"name"`
	Region      string  `json:"region"`
	Platform    string  `json:"platform"`
	State       string  `json:"state"`
	StateDetail *string `json:"state_detail"`
	CreatedAt   string  `json:"created_at"`

	Resources   *PoolResources   `json:"resources,omitempty"`
	Commitments []PoolCommitment `json:"commitments,omitempty"`
	Capacity    *PoolResources   `json:"capacity,omitempty"`
	Isolation   []string         `json:"isolation,omitempty"`
	AWS         *PoolAWS         `json:"aws,omitempty"`
}

// PoolList is one page of pools, newest first.
type PoolList struct {
	Pools      []Pool `json:"pools"`
	NextCursor string `json:"next_cursor,omitempty"`
}

// PoolUsage is what a pool's VMs currently hold.
type PoolUsage struct {
	PoolID               string        `json:"pool_id"`
	ResourcesAllocated   PoolResources `json:"resources_allocated"`
	AllocationObservedAt string        `json:"allocation_observed_at"`
}

// PoolWorker is one machine of an `aws` or `self_hosted` pool.
type PoolWorker struct {
	WorkerID        string          `json:"worker_id"`
	PoolID          string          `json:"pool_id"`
	Kind            string          `json:"kind"`
	Version         string          `json:"version"`
	Platform        string          `json:"platform"`
	State           string          `json:"state"`
	Live            bool            `json:"live"`
	Resources       WorkerResources `json:"resources"`
	Free            WorkerResources `json:"free"`
	LastHeartbeatAt string          `json:"last_heartbeat_at"`
}

// WorkerResources is what a pool worker contributes, or has free.
type WorkerResources struct {
	VCPU      int64 `json:"vcpu"`
	MemoryMiB int64 `json:"memory_mib"`
	DiskMiB   int64 `json:"disk_mib"`
	GPUCount  int64 `json:"gpu_count,omitempty"`
}

// PoolJoinKey lets machines join a `self_hosted` pool. Key is set only in the
// response that created it.
type PoolJoinKey struct {
	JoinKeyID string `json:"join_key_id"`
	Key       string `json:"key,omitempty"`
	CreatedAt string `json:"created_at"`
}

// ListPoolsOptions pages a pool listing.
type ListPoolsOptions struct {
	Cursor string
	Limit  int
}

// CreatePoolRequest is a new pool. Resources is required for `arker` and
// `aws`; AWS and Isolation are for `aws` only.
type CreatePoolRequest struct {
	Type        string           `json:"type"`
	Platform    string           `json:"platform"`
	Name        string           `json:"name,omitempty"`
	Resources   *PoolResources   `json:"resources,omitempty"`
	Commitments []PoolCommitment `json:"commitments,omitempty"`
	AWS         *CreatePoolAWS   `json:"aws,omitempty"`
	Isolation   []string         `json:"isolation,omitempty"`
}

// UpdatePoolRequest changes a pool. A nil field is left unchanged.
//
//	Name:        arker.Ptr("")                  // removes the name
//	Commitments: &[]arker.PoolCommitment{...}   // replaces the whole list
type UpdatePoolRequest struct {
	Name        *string
	Resources   *PoolResources
	Commitments *[]PoolCommitment
}

// ListPools pages the organization's pools in every region.
func (c *Client) ListPools(ctx context.Context, opts ListPoolsOptions) (*PoolList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	var out PoolList
	_, err := c.control(ctx, q.on("/v1/pools"), &out)
	return &out, err
}

// CreatePool creates a pool in the client's region. Requires an admin key. An
// `aws` pool stays "provisioning" until the role stack from AWS.SetupURL exists
// and its hosts have joined.
func (c *Client) CreatePool(ctx context.Context, req CreatePoolRequest) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPost, path: "/v1/pools", body: req, out: &out})
	return &out, err
}

// GetPool fetches one pool.
func (c *Client) GetPool(ctx context.Context, poolID string) (*Pool, error) {
	var out Pool
	_, err := c.control(ctx, poolPath(poolID), &out)
	return &out, err
}

// UpdatePool renames, resizes or replaces the commitments of a pool. Requires
// an admin key.
func (c *Client) UpdatePool(ctx context.Context, poolID string, req UpdatePoolRequest) (*Pool, error) {
	body := map[string]any{}
	switch {
	case req.Name == nil:
	case *req.Name == "":
		body["name"] = nil
	default:
		body["name"] = *req.Name
	}
	if req.Resources != nil {
		body["resources"] = req.Resources
	}
	if req.Commitments != nil {
		body["commitments"] = req.Commitments
	}
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPatch, path: poolPath(poolID), base: c.controlURL, body: body, out: &out})
	return &out, err
}

// AddPoolCommitment adds a commitment and keeps the pool's current ones, which
// UpdatePool would otherwise need resent unchanged. It is not atomic: a change
// made between its read and its write is lost. Requires an admin key.
func (c *Client) AddPoolCommitment(ctx context.Context, poolID string, commitment PoolCommitment) (*Pool, error) {
	pool, err := c.GetPool(ctx, poolID)
	if err != nil {
		return nil, err
	}
	commitments := append(pool.Commitments, commitment)
	return c.UpdatePool(ctx, poolID, UpdatePoolRequest{Commitments: &commitments})
}

// DeletePool starts deleting a pool and its VMs; it stays "deleting" until they
// are gone. Refused while a commitment is active. Requires an admin key.
func (c *Client) DeletePool(ctx context.Context, poolID string) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodDelete, path: poolPath(poolID), base: c.controlURL, out: &out})
	return &out, err
}

// GetPoolUsage reports the resources currently allocated to VMs in the pool.
func (c *Client) GetPoolUsage(ctx context.Context, poolID string) (*PoolUsage, error) {
	var out PoolUsage
	_, err := c.control(ctx, poolPath(poolID)+"/usage", &out)
	return &out, err
}

// ListPoolWorkers lists the machines of an `aws` or `self_hosted` pool that
// reported in the last day.
func (c *Client) ListPoolWorkers(ctx context.Context, poolID string) ([]PoolWorker, error) {
	var out struct {
		Workers []PoolWorker `json:"workers"`
	}
	_, err := c.control(ctx, poolPath(poolID)+"/workers", &out)
	return out.Workers, err
}

// CreatePoolJoinKey creates a key that lets a machine join a `self_hosted`
// pool. The returned Key is shown only this once. Requires an admin key.
func (c *Client) CreatePoolJoinKey(ctx context.Context, poolID string) (*PoolJoinKey, error) {
	var out PoolJoinKey
	_, err := c.do(ctx, call{method: http.MethodPost, path: poolPath(poolID) + "/join-keys", base: c.controlURL, out: &out})
	return &out, err
}

// ListPoolJoinKeys lists a `self_hosted` pool's join keys, newest first.
func (c *Client) ListPoolJoinKeys(ctx context.Context, poolID string) ([]PoolJoinKey, error) {
	var out struct {
		JoinKeys []PoolJoinKey `json:"join_keys"`
	}
	_, err := c.control(ctx, poolPath(poolID)+"/join-keys", &out)
	return out.JoinKeys, err
}

// RevokePoolJoinKey revokes a join key; machines using it leave the pool.
// Requires an admin key.
func (c *Client) RevokePoolJoinKey(ctx context.Context, poolID, joinKeyID string) error {
	_, err := c.do(ctx, call{
		method: http.MethodDelete, path: poolPath(poolID) + "/join-keys/" + segment(joinKeyID), base: c.controlURL,
	})
	return err
}

func poolPath(poolID string) string { return "/v1/pools/" + segment(poolID) }
