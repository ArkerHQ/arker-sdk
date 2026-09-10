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
// Keep it minimal: Arker hashes the whole canonicalised request into the
// idempotency identity, so a field that varies between two otherwise identical
// retries turns a replay into a 409.
type ForkRequest struct {
	SourceVMID   string `json:"source_vm_id,omitempty"`
	SourceVMName string `json:"source_vm_name,omitempty"`
	SourceOrgID  string `json:"source_org_id,omitempty"`
	Image        string `json:"image,omitempty"`
	Dockerfile   string `json:"dockerfile,omitempty"`

	Name            string     `json:"name,omitempty"`
	Description     string     `json:"description,omitempty"`
	Resources       *Resources `json:"resources,omitempty"`
	Platforms       []string   `json:"platforms,omitempty"`
	Layers          []string   `json:"layers,omitempty"`
	Durable         *bool      `json:"durable,omitempty"`
	QueueingTimeout *int       `json:"queueing_timeout,omitempty"`

	// IdempotencyKey makes the fork replayable. Empty generates one per call,
	// enough to make the SDK's own retries safe. Set it to survive across
	// processes -- the only form that protects your retry after an
	// UnknownOutcomeError, since a fresh Fork otherwise mints a new key.
	// A header, never a body field: unknown body fields are rejected.
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
	// State is exactly {"idle","running"} and means "is a command in flight",
	// not "is the VM awake": a suspended VM also reports "idle".
	State     string     `json:"state,omitempty"`
	Resources *Resources `json:"resources,omitempty"`
	Sessions  []Session  `json:"sessions,omitempty"`
}

// VM is a handle to one machine, mirroring `vm` in the Python and TypeScript
// SDKs. Info is nil on a bare handle from Client.VM until Refresh.
type VM struct {
	ID     string
	Info   *VMInfo
	client *Client
}

func (v *VM) path(suffix string) string { return "/v1/vms/" + v.ID + suffix }

// VM returns a handle without contacting the API.
func (c *Client) VM(vmID string) *VM { return &VM{ID: vmID, client: c} }

// Fork creates a VM from a source.
//
// Every fork carries an Idempotency-Key because the retry it guards against is
// the SDK's own: a 502/504 is a response, so it is retried even on a mutation,
// and the origin may already have built the VM.
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
	return &VM{info.VMID, &info, c}, nil
}

// GetVM fetches one VM. found is false on 404.
func (c *Client) GetVM(ctx context.Context, vmID string) (*VM, bool, error) {
	vm := c.VM(vmID)
	switch err := vm.Refresh(ctx); {
	case IsNotFound(err):
		return nil, false, nil
	case err != nil:
		return nil, false, err
	}
	return vm, true, nil
}

// ListVMsOptions narrows a listing.
type ListVMsOptions struct {
	Limit  int
	Cursor string
	State  string
}

// ListVMs returns one page of VMs plus the cursor for the next.
func (c *Client) ListVMs(ctx context.Context, opts ListVMsOptions) ([]*VM, string, error) {
	query := url.Values{}
	for key, value := range map[string]string{
		"limit": strconv.Itoa(opts.Limit), "cursor": opts.Cursor, "state": opts.State,
	} {
		if value != "" && value != "0" {
			query.Set(key, value)
		}
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
	vms := make([]*VM, 0, len(out.VMs))
	for i := range out.VMs {
		vms = append(vms, &VM{out.VMs[i].VMID, &out.VMs[i], c})
	}
	return vms, out.NextCursor, nil
}

// Refresh re-reads this VM into Info.
func (v *VM) Refresh(ctx context.Context) error {
	var info VMInfo
	if _, err := v.client.do(ctx, http.MethodGet, v.path(""), nil, "", &info); err != nil {
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

// Delete removes this VM. Retry-safe: an already-absent VM is success.
func (v *VM) Delete(ctx context.Context) error {
	status, err := v.client.do(ctx, http.MethodDelete, v.path(""), nil, "", nil)
	if status == http.StatusNotFound {
		return nil
	}
	return err
}
