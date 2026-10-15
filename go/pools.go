package arker

import (
	"context"
	"errors"
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

type CreatePoolAWS struct {
	AccountID        string   `json:"account_id"`
	SubnetIDs        []string `json:"subnet_ids"`
	SecurityGroupIDs []string `json:"security_group_ids,omitempty"`
	InstanceTypes    []string `json:"instance_types,omitempty"`
}

type PoolAWS struct {
	AccountID        string   `json:"account_id"`
	SubnetIDs        []string `json:"subnet_ids"`
	SecurityGroupIDs []string `json:"security_group_ids"`
	InstanceTypes    []string `json:"instance_types"`
	RoleName         string   `json:"role_name"`
	ExternalID       string   `json:"external_id"`
	SetupURL         string   `json:"setup_url"`
}

// Pool.Type is arker, aws, or self_hosted. Resources and Commitments belong
// to arker/aws pools; Capacity belongs to aws/self_hosted pools; AWS to aws.
type Pool struct {
	PoolID      string           `json:"pool_id"`
	Type        string           `json:"type"`
	Name        *string          `json:"name"`
	Region      string           `json:"region"`
	Platform    string           `json:"platform"`
	State       string           `json:"state"`
	StateDetail *string          `json:"state_detail"`
	CreatedAt   string           `json:"created_at"`
	Resources   *PoolResources   `json:"resources,omitempty"`
	Commitments []PoolCommitment `json:"commitments,omitempty"`
	Capacity    *PoolResources   `json:"capacity,omitempty"`
	Isolation   []string         `json:"isolation,omitempty"`
	AWS         *PoolAWS         `json:"aws,omitempty"`
}

type PoolList struct {
	Pools      []Pool  `json:"pools"`
	NextCursor *string `json:"next_cursor"`
}

type PoolUsage struct {
	PoolID               string        `json:"pool_id"`
	ResourcesAllocated   PoolResources `json:"resources_allocated"`
	AllocationObservedAt string        `json:"allocation_observed_at"`
}

type ListPoolsOptions struct {
	Cursor string
	Limit  int
}

// CreatePoolRequest creates in the client's region. Resources are required for
// arker/aws, AWS for aws; self_hosted accepts only Type, Platform, and Name.
type CreatePoolRequest struct {
	Type        string           `json:"type"`
	Platform    string           `json:"platform"`
	Name        string           `json:"name,omitempty"`
	Resources   *PoolResources   `json:"resources,omitempty"`
	Commitments []PoolCommitment `json:"commitments,omitempty"`
	Isolation   []string         `json:"isolation,omitempty"`
	AWS         *CreatePoolAWS   `json:"aws,omitempty"`
}

// UpdatePoolRequest changes supplied fields. RenamePool can clear a name.
// A pointer to an empty commitment slice clears all unstarted commitments.
type UpdatePoolRequest struct {
	Name        *string           `json:"name,omitempty"`
	Resources   *PoolResources    `json:"resources,omitempty"`
	Commitments *[]PoolCommitment `json:"commitments,omitempty"`
}

type PoolJoinKey struct {
	JoinKeyID string `json:"join_key_id"`
	CreatedAt string `json:"created_at"`
}
type CreatedPoolJoinKey struct {
	JoinKeyID string `json:"join_key_id"`
	CreatedAt string `json:"created_at"`
	Key       string `json:"key"`
}
type PoolJoinKeyList struct {
	JoinKeys []PoolJoinKey `json:"join_keys"`
}

type WorkerResources struct {
	VCPU      int64  `json:"vcpu"`
	MemoryMiB int64  `json:"memory_mib"`
	DiskMiB   int64  `json:"disk_mib"`
	GPUCount  *int64 `json:"gpu_count,omitempty"`
}
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
func (c *Client) GetPoolUsage(ctx context.Context, poolID string) (*PoolUsage, error) {
	var out PoolUsage
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/pools/" + segment(poolID) + "/usage", out: &out})
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
	if pool.Type == "self_hosted" {
		return nil, errors.New("arker: self_hosted pools do not support commitments")
	}
	commitments := append(pool.Commitments, commitment)
	return c.UpdatePool(ctx, poolID, UpdatePoolRequest{Commitments: &commitments})
}
func (c *Client) ListPoolJoinKeys(ctx context.Context, poolID string) (*PoolJoinKeyList, error) {
	var out PoolJoinKeyList
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/pools/" + segment(poolID) + "/join-keys", out: &out})
	return &out, err
}
func (c *Client) CreatePoolJoinKey(ctx context.Context, poolID string) (*CreatedPoolJoinKey, error) {
	var out CreatedPoolJoinKey
	_, err := c.do(ctx, call{method: http.MethodPost, path: "/v1/pools/" + segment(poolID) + "/join-keys", out: &out})
	return &out, err
}
func (c *Client) DeletePoolJoinKey(ctx context.Context, poolID, joinKeyID string) error {
	_, err := c.do(ctx, call{method: http.MethodDelete, path: "/v1/pools/" + segment(poolID) + "/join-keys/" + segment(joinKeyID)})
	return err
}
func (c *Client) ListPoolWorkers(ctx context.Context, poolID string) (*PoolWorkerList, error) {
	var out PoolWorkerList
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/pools/" + segment(poolID) + "/workers", out: &out})
	return &out, err
}
