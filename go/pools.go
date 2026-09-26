package arker

import (
	"context"
	"errors"
	"net/http"
)

// Pools are org-scoped and served by the control plane. They are available to
// organizations with pools enabled; others get `not_found`.

// PoolResources is reserved capacity. At least one quantity must be positive.
type PoolResources struct {
	VCPU      int64 `json:"vcpu,omitempty"`
	MemoryMiB int64 `json:"memory_mib,omitempty"`
	DiskMiB   int64 `json:"disk_mib,omitempty"`
}

// Pool is prepaid capacity in one provider and region for a fixed term.
type Pool struct {
	PoolID          string        `json:"pool_id"`
	Name            *string       `json:"name"`
	Provider        string        `json:"provider"`
	Region          string        `json:"region"`
	Resources       PoolResources `json:"resources"`
	DurationSeconds int64         `json:"duration_seconds"`
	// Status is "active", or "expired" once EndsAt has passed.
	Status      string  `json:"status"`
	CreatedAt   string  `json:"created_at"`
	StartsAt    string  `json:"starts_at"`
	EndsAt      string  `json:"ends_at"`
	Currency    string  `json:"currency"`
	AmountCents int64   `json:"amount_cents"`
	InvoiceID   *string `json:"invoice_id"`
}

// PoolList is one page of pools, newest first.
type PoolList struct {
	Pools            []Pool `json:"pools"`
	PurchasesEnabled bool   `json:"purchases_enabled"`
	NextCursor       string `json:"next_cursor,omitempty"`
}

// PoolUsage is what a pool's VMs currently hold.
type PoolUsage struct {
	PoolID               string        `json:"pool_id"`
	ResourcesAllocated   PoolResources `json:"resources_allocated"`
	AllocationObservedAt *string       `json:"allocation_observed_at"`
}

// ListPoolsOptions pages a pool listing.
type ListPoolsOptions struct {
	Cursor string
	Limit  int
}

// CreatePoolRequest is what to buy. Provider and Region default to the
// client's placement.
type CreatePoolRequest struct {
	Provider        string        `json:"provider"`
	Region          string        `json:"region"`
	Resources       PoolResources `json:"resources"`
	DurationSeconds int64         `json:"duration_seconds"`
	Name            string        `json:"name,omitempty"`
	// IdempotencyKey makes a purchase replayable across processes. Without
	// one, CreatePool generates a key per call, so its own retries can never
	// buy twice.
	IdempotencyKey string `json:"-"`
}

type poolPurchase struct {
	CreatePoolRequest
	AmountCents int64 `json:"amount_cents"`
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

// RenamePool sets a pool's name; nil removes it. Requires an admin key.
func (c *Client) RenamePool(ctx context.Context, poolID string, name *string) (*Pool, error) {
	var out Pool
	_, err := c.do(ctx, call{
		method: http.MethodPatch, path: "/v1/pools/" + segment(poolID), base: c.controlURL,
		body: map[string]*string{"name": name}, out: &out,
	})
	return &out, err
}

// CreatePool buys a pool at the current price, billed on the organization's
// next invoice. Requires an admin key.
//
// It prices the pool, then buys at exactly that price: if the price changes in
// between, it returns a `conflict` rather than paying a different amount.
func (c *Client) CreatePool(ctx context.Context, req CreatePoolRequest) (*Pool, error) {
	req.Provider = firstNonEmpty(req.Provider, c.provider)
	req.Region = firstNonEmpty(req.Region, c.region)
	if req.Provider == "" || req.Region == "" {
		return nil, errors.New("arker: Provider and Region are required for a pool; set them or configure the client's placement")
	}
	var quote struct {
		AmountCents int64 `json:"amount_cents"`
	}
	if _, err := c.do(ctx, call{
		method: http.MethodPost, path: "/v1/pools/quote", base: c.controlURL, body: req, out: &quote,
	}); err != nil {
		return nil, err
	}
	key := req.IdempotencyKey
	if key == "" {
		key = newIdempotencyKey("sdk-pool-")
	}
	var out Pool
	_, err := c.do(ctx, call{
		method: http.MethodPost, path: "/v1/pools", base: c.controlURL,
		body: poolPurchase{req, quote.AmountCents}, key: key, out: &out,
		// The key binds every attempt to one purchase, so a retry after a lost
		// response returns the original pool instead of buying another.
		retryNetwork: true,
	})
	return &out, err
}
