package arker

import (
	"context"
	"fmt"
	"net/http"
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
	// It is part of the request body and therefore part of the identity hash,
	// so keep it constant across retries of one logical fork.
	QueueingTimeout *int `json:"queueing_timeout,omitempty"`

	// IdempotencyKey makes this fork replayable. Leave it empty and the SDK
	// generates one per call, which is enough to make ITS OWN retries safe.
	// Set it to make the guarantee survive across processes -- the only form
	// that protects a caller's own retry after an UnknownOutcomeError.
	//
	// Sent as a header, never in the body: the server's validator rejects
	// unknown body fields.
	IdempotencyKey string `json:"-"`
}

// VM is a virtual machine as the API reports it.
type VM struct {
	VMID        string `json:"vm_id"`
	Name        string `json:"name,omitempty"`
	Description string `json:"description,omitempty"`
	OwnerOrgID  string `json:"owner_org_id,omitempty"`
	Hostname    string `json:"hostname,omitempty"`
	CreatedAt   string `json:"created_at,omitempty"`
	Region      string `json:"region,omitempty"`
	Provider    string `json:"provider,omitempty"`
	Platform    string `json:"platform,omitempty"`
	// State is the unified lifecycle field, and its vocabulary is exactly
	// {"idle","running"}: it describes whether a COMMAND is in flight, not
	// whether the VM is awake. A suspended VM and a healthy unoccupied one
	// both report "idle".
	State     string     `json:"state,omitempty"`
	Resources *Resources `json:"resources,omitempty"`
}

// Fork creates a VM from a source and returns it.
//
// Every fork carries an Idempotency-Key. It is generated per call rather than
// demanded from the caller because the retry it guards against is the SDK's
// own: a 502 or 504 is a RESPONSE, so it is retried even on a mutation, and
// the origin may already have built the VM. Without a key that retry builds a
// SECOND machine while the first runs on, unnamed and billable. The key is
// bound once, before the retry loop, so every attempt presents the same one.
//
// Pass ForkRequest.IdempotencyKey to extend the guarantee across processes.
func (c *Client) Fork(ctx context.Context, req ForkRequest) (*VM, error) {
	key := req.IdempotencyKey
	if strings.TrimSpace(key) == "" {
		key = newIdempotencyKey("sdk-fork-")
	}
	var vm VM
	if _, err := c.do(ctx, http.MethodPost, "/v1/fork", req, key, &vm); err != nil {
		return nil, err
	}
	if strings.TrimSpace(vm.VMID) == "" {
		return nil, fmt.Errorf("arker: fork returned no vm_id")
	}
	return &vm, nil
}

// GetVM fetches one VM. found is false on 404 -- which is ORG-SCOPED, so a VM
// in another org is indistinguishable from a deleted one.
func (c *Client) GetVM(ctx context.Context, vmID string) (vm *VM, found bool, err error) {
	var out VM
	status, err := c.do(ctx, http.MethodGet, "/v1/vms/"+vmID, nil, "", &out)
	if status == http.StatusNotFound {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return &out, true, nil
}

// DeleteVM removes a VM. It is retry-safe: an already-absent VM is success,
// not failure, so a caller reconciling after an ambiguous error can call it
// freely.
func (c *Client) DeleteVM(ctx context.Context, vmID string) error {
	status, err := c.do(ctx, http.MethodDelete, "/v1/vms/"+vmID, nil, "", nil)
	if status == http.StatusNotFound {
		return nil
	}
	return err
}
