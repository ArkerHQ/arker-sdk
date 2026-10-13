package arker

import (
	"context"
	"encoding/json"
	"net/http"
)

// Pools are regional: CreatePool, UpdatePool, DeletePool and ListPoolWorkers go
// to the client's regional base URL (Provider and Region, or BaseURL). Listing
// and fetching go to the control plane, which finds the pool's region.

// PoolResources is capacity in the same units as VM resources.
type PoolResources struct {
	VCPU      int64 `json:"vcpu,omitempty"`
	MemoryMiB int64 `json:"memory_mib,omitempty"`
	DiskMiB   int64 `json:"disk_mib,omitempty"`
}

// PoolCommitment is a period the pool is committed to. Both ends are
// inclusive (RFC 3339) and the period lasts at least 24 hours. While one is
// active the pool cannot shrink or be deleted.
type PoolCommitment struct {
	StartsAt string `json:"starts_at"`
	EndsAt   string `json:"ends_at"`
}

// Pool compute types.
const (
	PoolComputeManaged = "managed"
	PoolComputeAWS     = "aws"
)

// PoolCompute says whose compute runs the pool's VMs: Arker's fleet
// (Type "managed", the default) or hosts Arker launches in your AWS account
// (Type "aws", which needs AccountID and SubnetIDs).
//
// An "aws" pool stays "provisioning" until the role Arker assumes exists: open
// SetupURL while signed in to the account to create it. RoleARN defaults to
// that role.
type PoolCompute struct {
	Type      string `json:"type"`
	AccountID string `json:"account_id,omitempty"`
	// RoleARN must name an ArkerPool* role in AccountID. Empty means the role
	// SetupURL creates for this pool.
	RoleARN          string   `json:"role_arn,omitempty"`
	Region           string   `json:"region,omitempty"`
	SubnetIDs        []string `json:"subnet_ids,omitempty"`
	SecurityGroupIDs []string `json:"security_group_ids,omitempty"`
	InstanceTypes    []string `json:"instance_types,omitempty"`
	// ExternalID and SetupURL are set by Arker and ignored on create.
	ExternalID string  `json:"external_id,omitempty"`
	SetupURL   *string `json:"setup_url,omitempty"`
}

// Pool isolation classes: state a pool keeps for itself instead of in Arker's
// shared regional stores. Metadata and logs need "aws" compute; VMs need "aws"
// compute or a joinable pool, whose workers then keep VM storage themselves.
const (
	PoolIsolationVMs      = "vms"
	PoolIsolationLogs     = "logs"
	PoolIsolationMetadata = "metadata"
)

// PoolCapacity is what the pool can hold right now.
type PoolCapacity struct {
	VCPU      int64 `json:"vcpu"`
	MemoryMiB int64 `json:"memory_mib"`
	DiskMiB   int64 `json:"disk_mib"`
	// Forks is set for a fork-reserve pool only.
	Forks *int64 `json:"forks"`
}

// Pool is a group of VMs sharing capacity in one provider and region.
type Pool struct {
	PoolID   string  `json:"pool_id"`
	Name     *string `json:"name"`
	Provider string  `json:"provider"`
	Region   string  `json:"region"`
	// State is "provisioning", "idle" or "running".
	State string `json:"state"`
	// StateDetail says why the pool is still provisioning, when something is
	// wrong.
	StateDetail *string          `json:"state_detail"`
	Resources   PoolResources    `json:"resources"`
	Capacity    PoolCapacity     `json:"capacity"`
	Commitments []PoolCommitment `json:"commitments"`
	Compute     PoolCompute      `json:"compute"`
	Joinable    bool             `json:"joinable"`
	// Isolation holds PoolIsolation* classes.
	Isolation  []string `json:"isolation"`
	SourceVMID *string  `json:"source_vm_id"`
	MaxForks   *int64   `json:"max_forks"`
	CreatedAt  string   `json:"created_at"`
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
	AllocationObservedAt *string       `json:"allocation_observed_at"`
}

// WorkerResources is a worker's capacity.
type WorkerResources struct {
	VCPU      int64 `json:"vcpu"`
	MemoryMiB int64 `json:"memory_mib"`
	DiskMiB   int64 `json:"disk_mib"`
	GPUCount  int64 `json:"gpu_count,omitempty"`
}

// Worker is a host heartbeating into a pool.
type Worker struct {
	WorkerID        string          `json:"worker_id"`
	PoolID          string          `json:"pool_id"`
	Kind            string          `json:"kind"` // the worker's software, such as "arkerd"
	Version         string          `json:"version"`
	Platforms       []string        `json:"platforms"`
	State           string          `json:"state"` // "active", "draining" or "left"
	Resources       WorkerResources `json:"resources"`
	Free            WorkerResources `json:"free"`
	LastHeartbeatAt string          `json:"last_heartbeat_at"`
}

// ListPoolsOptions pages a pool listing.
type ListPoolsOptions struct {
	Cursor string
	Limit  int
}

// CreatePoolRequest describes a pool. Everything is optional: a pool with no
// compute is managed. Set SourceVMID and MaxForks together for a fork-reserve
// pool.
type CreatePoolRequest struct {
	Name        string           `json:"name,omitempty"`
	Resources   *PoolResources   `json:"resources,omitempty"`
	Commitments []PoolCommitment `json:"commitments,omitempty"`
	Compute     *PoolCompute     `json:"compute,omitempty"`
	// Joinable lets workers holding a worker key for this pool add capacity.
	Joinable bool `json:"joinable,omitempty"`
	// Isolation lists PoolIsolation* classes the pool keeps for itself. It
	// cannot change after creation.
	Isolation  []string `json:"isolation,omitempty"`
	SourceVMID string   `json:"source_vm_id,omitempty"`
	MaxForks   int64    `json:"max_forks,omitempty"`
}

// UpdatePoolRequest changes a pool. Only set fields are sent.
type UpdatePoolRequest struct {
	Name *string
	// ClearName removes the name; it wins over Name.
	ClearName bool
	Resources *PoolResources
	// Commitments is the full list, and started commitments must be kept
	// unchanged. Nil leaves them alone; an empty non-nil slice sends none.
	Commitments []PoolCommitment
	Joinable    *bool
	MaxForks    *int64
}

// MarshalJSON sends only the fields that were set, which a plain struct tag
// cannot do for a name that may be cleared or a commitment list that may be
// emptied.
func (r UpdatePoolRequest) MarshalJSON() ([]byte, error) {
	m := map[string]any{}
	switch {
	case r.ClearName:
		m["name"] = nil
	case r.Name != nil:
		m["name"] = *r.Name
	}
	if r.Resources != nil {
		m["resources"] = r.Resources
	}
	if r.Commitments != nil {
		m["commitments"] = r.Commitments
	}
	if r.Joinable != nil {
		m["joinable"] = *r.Joinable
	}
	if r.MaxForks != nil {
		m["max_forks"] = *r.MaxForks
	}
	return json.Marshal(m)
}

// ListPools pages the organization's pools.
func (c *Client) ListPools(ctx context.Context, opts ListPoolsOptions) (*PoolList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	var out PoolList
	_, err := c.control(ctx, q.on("/v1/pools"), &out)
	return &out, err
}

// GetPool fetches one pool.
func (c *Client) GetPool(ctx context.Context, poolID string) (*Pool, error) {
	var out Pool
	_, err := c.control(ctx, "/v1/pools/"+segment(poolID), &out)
	return &out, err
}

// GetPoolUsage reports the resources currently allocated to VMs in the pool.
func (c *Client) GetPoolUsage(ctx context.Context, poolID string) (*PoolUsage, error) {
	var out PoolUsage
	_, err := c.control(ctx, "/v1/pools/"+segment(poolID)+"/usage", &out)
	return &out, err
}

// CreatePool creates a pool in the client's region. Requires an admin key.
func (c *Client) CreatePool(ctx context.Context, req CreatePoolRequest) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPost, path: "/v1/pools", body: req, out: &out})
	return &out, err
}

// UpdatePool changes a pool. Requires an admin key.
func (c *Client) UpdatePool(ctx context.Context, poolID string, req UpdatePoolRequest) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{method: http.MethodPatch, path: "/v1/pools/" + segment(poolID), body: req, out: &out})
	return &out, err
}

// DeletePool deletes a pool and every VM in it. It is refused while a
// commitment is active. Requires an admin key.
func (c *Client) DeletePool(ctx context.Context, poolID string) error {
	_, err := c.do(ctx, call{method: http.MethodDelete, path: "/v1/pools/" + segment(poolID)})
	return err
}

// ListPoolWorkers lists the workers that heartbeated into the pool within the
// last day.
func (c *Client) ListPoolWorkers(ctx context.Context, poolID string) ([]Worker, error) {
	var out struct {
		Workers []Worker `json:"workers"`
	}
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/pools/" + segment(poolID) + "/workers", out: &out})
	return out.Workers, err
}
