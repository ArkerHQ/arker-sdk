package arker

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// Resources sizes a VM. Omit a field to inherit the source's value.
type Resources struct {
	VCPU      *int     `json:"vcpu,omitempty"`
	MemoryMiB *int     `json:"memory_mib,omitempty"`
	DiskMiB   *int     `json:"disk_mib,omitempty"`
	VGPU      *float64 `json:"vgpu,omitempty"`
}

// ForkRequest creates a VM from exactly one source.
//
// Keep it minimal. Arker hashes the WHOLE canonicalised request into the
// idempotency identity, so any field that varies between two otherwise
// identical retries turns a replay into a 409.
type ForkRequest struct {
	SourceVMID   string `json:"source_vm_id,omitempty"`
	SourceVMName string `json:"source_vm_name,omitempty"`
	SourceOrgID  string `json:"source_org_id,omitempty"`
	Image        string `json:"image,omitempty"`
	Dockerfile   string `json:"dockerfile,omitempty"`

	Name        string     `json:"name,omitempty"`
	Description string     `json:"description,omitempty"`
	Resources   *Resources `json:"resources,omitempty"`
	Platforms   []string   `json:"platforms,omitempty"`
	Layers      []string   `json:"layers,omitempty"`
	Durable     *bool      `json:"durable,omitempty"`

	// QueueingTimeout queues instead of failing fast when there is no room.
	// It is part of the body and therefore part of the identity hash, so keep
	// it constant across retries of one logical fork.
	QueueingTimeout *int `json:"queueing_timeout,omitempty"`

	// IdempotencyKey makes this fork replayable. Leave it empty and one is
	// generated per call, which is enough to make the SDK's OWN retries safe.
	// Set it to survive across processes -- the only form that protects your
	// own retry after an UnknownOutcomeError, since a fresh Fork call
	// otherwise mints a new key and the server sees an unrelated request.
	//
	// Sent as a header, never in the body: the server's validator rejects
	// unknown body fields.
	IdempotencyKey string `json:"-"`
}

// VMInfo is a virtual machine as the API reports it.
type VMInfo struct {
	VMID        string `json:"vm_id"`
	Name        string `json:"name,omitempty"`
	Description string `json:"description,omitempty"`
	OwnerOrgID  string `json:"owner_org_id,omitempty"`
	Hostname    string `json:"hostname,omitempty"`
	CreatedAt   string `json:"created_at,omitempty"`
	Region      string `json:"region,omitempty"`
	Provider    string `json:"provider,omitempty"`
	Platform    string `json:"platform,omitempty"`
	// State's vocabulary is exactly {"idle","running"} and describes whether a
	// COMMAND is in flight -- not whether the VM is awake. A suspended VM and
	// a healthy unoccupied one both report "idle".
	State     string     `json:"state,omitempty"`
	Resources *Resources `json:"resources,omitempty"`
	Sessions  []Session  `json:"sessions,omitempty"`
}

// VM is a handle to one machine, mirroring `vm` in the Python and TypeScript
// SDKs: methods hang off the handle rather than taking an id.
//
// Info is populated by Fork and GetVM, and is nil on a bare handle from
// Client.VM until you call Refresh.
type VM struct {
	ID     string
	Info   *VMInfo
	client *Client
}

// VM returns a handle without contacting the API. Call Refresh to populate it.
func (c *Client) VM(vmID string) *VM {
	return &VM{ID: vmID, client: c}
}

// Fork creates a VM from a source and returns a handle to it.
//
// Every fork carries an Idempotency-Key, generated per call, because the retry
// it guards against is the SDK's own: a 502 or 504 is a RESPONSE, so it is
// retried even on a mutation, and the origin may already have built the VM.
// The key is bound once, before the retry loop, so every attempt presents the
// same one and the server replays instead of building a second machine.
func (c *Client) Fork(ctx context.Context, req ForkRequest) (*VM, error) {
	key := req.IdempotencyKey
	if strings.TrimSpace(key) == "" {
		key = newIdempotencyKey("sdk-fork-")
	}
	var info VMInfo
	if _, err := c.do(ctx, http.MethodPost, "/v1/fork", req, key, &info); err != nil {
		return nil, err
	}
	if strings.TrimSpace(info.VMID) == "" {
		return nil, fmt.Errorf("arker: fork returned no vm_id")
	}
	return &VM{ID: info.VMID, Info: &info, client: c}, nil
}

// GetVM fetches one VM. found is false on 404 -- which is ORG-SCOPED, so a VM
// in another org is indistinguishable from a deleted one.
func (c *Client) GetVM(ctx context.Context, vmID string) (vm *VM, found bool, err error) {
	var info VMInfo
	status, err := c.do(ctx, http.MethodGet, "/v1/vms/"+vmID, nil, "", &info)
	if status == http.StatusNotFound {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return &VM{ID: info.VMID, Info: &info, client: c}, true, nil
}

// ListVMsOptions narrows a listing.
type ListVMsOptions struct {
	Limit  int
	Cursor string
	State  string
}

// ListVMs returns one page of VMs plus the cursor for the next.
func (c *Client) ListVMs(ctx context.Context, opts ListVMsOptions) (vms []*VM, nextCursor string, err error) {
	query := url.Values{}
	if opts.Limit > 0 {
		query.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Cursor != "" {
		query.Set("cursor", opts.Cursor)
	}
	if opts.State != "" {
		query.Set("state", opts.State)
	}
	path := "/v1/vms"
	if encoded := query.Encode(); encoded != "" {
		path += "?" + encoded
	}
	var out struct {
		VMs        []VMInfo `json:"vms"`
		NextCursor string   `json:"next_cursor"`
	}
	if _, err := c.do(ctx, http.MethodGet, path, nil, "", &out); err != nil {
		return nil, "", err
	}
	for i := range out.VMs {
		info := out.VMs[i]
		vms = append(vms, &VM{ID: info.VMID, Info: &info, client: c})
	}
	return vms, out.NextCursor, nil
}

// Refresh re-reads this VM and updates Info in place.
func (v *VM) Refresh(ctx context.Context) error {
	var info VMInfo
	if _, err := v.client.do(ctx, http.MethodGet, "/v1/vms/"+v.ID, nil, "", &info); err != nil {
		return err
	}
	v.Info = &info
	return nil
}

// Fork creates a child of this VM.
func (v *VM) Fork(ctx context.Context, req ForkRequest) (*VM, error) {
	req.SourceVMID = v.ID
	return v.client.Fork(ctx, req)
}

// Delete removes this VM. It is retry-safe: an already-absent VM is success,
// so a caller reconciling after an ambiguous error can call it freely.
func (v *VM) Delete(ctx context.Context) error {
	status, err := v.client.do(ctx, http.MethodDelete, "/v1/vms/"+v.ID, nil, "", nil)
	if status == http.StatusNotFound {
		return nil
	}
	return err
}
