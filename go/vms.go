package arker

import (
	"context"
	"errors"
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

// VMResources is the resource shape the API reports back.
type VMResources struct {
	VCPU       *int `json:"vcpu,omitempty"`
	MemoryMiB  *int `json:"memory_mib,omitempty"`
	DiskMiB    *int `json:"disk_mib,omitempty"`
	GPUSMs     *int `json:"gpu_sms,omitempty"`
	GPUVRAMMiB *int `json:"gpu_vram_mib,omitempty"`
	GPUCount   *int `json:"gpu_count,omitempty"`
}

// RegistryAuth pulls Image from a private registry. Used for that one pull and
// never stored.
type RegistryAuth struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

// SSHPublicKeyInfo is one authorized key as the API reports it.
type SSHPublicKeyInfo struct {
	PublicKey   string `json:"public_key"`
	Fingerprint string `json:"fingerprint"`
}

// VMNetwork carries the VM's authorized SSH keys.
type VMNetwork struct {
	SSHPublicKeys []SSHPublicKeyInfo `json:"ssh_public_keys,omitempty"`
}

// ForkRequest creates a VM from exactly one source: SourceVMID, SourceVMName,
// or Image.
//
// Arker hashes the whole canonicalised request into the idempotency identity,
// so a field that varies between two otherwise identical retries turns a replay
// into a 409.
type ForkRequest struct {
	SourceVMID    string `json:"source_vm_id,omitempty"`
	SourceVMName  string `json:"source_vm_name,omitempty"`
	SourceOrgID   string `json:"source_org_id,omitempty"`
	SourceOrgName string `json:"source_org_name,omitempty"`
	Image         string `json:"image,omitempty"`

	Name          string     `json:"name,omitempty"`
	Description   string     `json:"description,omitempty"`
	Public        *bool      `json:"public,omitempty"`
	SSHPublicKeys []string   `json:"ssh_public_keys,omitempty"`
	Resources     *Resources `json:"resources,omitempty"`
	Platforms     []string   `json:"platforms,omitempty"`
	// Layers is the state inherited from the source. Omit it, or pass
	// ["disk","memory"], for a warm fork that resumes where the source left off.
	Layers          []string      `json:"layers,omitempty"`
	Disk            *bool         `json:"disk,omitempty"`
	Durable         *bool         `json:"durable,omitempty"`
	NestedVirt      *bool         `json:"nestedvirt,omitempty"`
	QueueingTimeout *int          `json:"queueing_timeout,omitempty"`
	Policies        *PolicyDoc    `json:"policies,omitempty"`
	RegistryAuth    *RegistryAuth `json:"registry_auth,omitempty"`

	// Dockerfile is rejected by the API by design -- the SDKs build it
	// client-side. Go does not implement that build yet; see Client.Fork.
	Dockerfile string `json:"-"`

	// IdempotencyKey makes the fork replayable. Empty generates one per call,
	// enough to make the SDK's own retries safe. Set it to survive across
	// processes -- the only form that protects your retry after an
	// UnknownOutcomeError, since a fresh Fork otherwise mints a new key.
	// A header, never a body field: unknown body fields are rejected.
	IdempotencyKey string `json:"-"`
}

// VMInfo is a virtual machine as the API reports it, mirroring the contract Vm.
type VMInfo struct {
	VMID                string   `json:"vm_id"`
	Name                string   `json:"name,omitempty"`
	Description         string   `json:"description,omitempty"`
	OwnerOrgID          string   `json:"owner_org_id,omitempty"`
	Hostname            string   `json:"hostname,omitempty"`
	CreatedAt           string   `json:"created_at,omitempty"`
	LastActiveAt        string   `json:"last_active_at,omitempty"`
	Public              bool     `json:"public,omitempty"`
	Region              string   `json:"region,omitempty"`
	Provider            string   `json:"provider,omitempty"`
	Platform            string   `json:"platform,omitempty"`
	RootSourceVMID      string   `json:"root_source_vm_id,omitempty"`
	RootSourceVMName    string   `json:"root_source_vm_name,omitempty"`
	CompatiblePlatforms []string `json:"compatible_platforms,omitempty"`
	GPUPlatforms        []string `json:"gpu_platforms,omitempty"`
	MaxVCPUs            *int     `json:"max_vcpus,omitempty"`
	MinVCPUs            *int     `json:"min_vcpus,omitempty"`
	MaxMemoryMiB        *int     `json:"max_memory_mib,omitempty"`
	MinMemoryMiB        *int     `json:"min_memory_mib,omitempty"`
	MaxDiskMiB          *int     `json:"max_disk_mib,omitempty"`
	MinDiskMiB          *int     `json:"min_disk_mib,omitempty"`
	// State is exactly {"idle","running"} and means "is a command in flight",
	// not "is the VM awake": a suspended VM also reports "idle".
	State     string       `json:"state,omitempty"`
	Network   *VMNetwork   `json:"network,omitempty"`
	Resources *VMResources `json:"resources,omitempty"`
	Sessions  []Session    `json:"sessions,omitempty"`
}

// VM is a handle to one machine. Info is nil on a bare handle from Client.VM
// until Refresh. baseURL is the VM's own regional endpoint, which is not always
// the client's: ListVMs aggregates across regions.
type VM struct {
	ID      string
	Info    *VMInfo
	client  *Client
	baseURL string
}

func (v *VM) path(suffix string) string { return vmPath(v.ID, suffix) }

func (v *VM) do(ctx context.Context, method, path string, body, out any) (int, error) {
	return v.client.do(ctx, call{method: method, path: path, base: v.baseURL, body: body, out: out})
}

// BaseURL is the regional endpoint this VM is served from.
func (v *VM) BaseURL() string { return v.baseURL }

// VM returns a handle without contacting the API.
func (c *Client) VM(vmID string) *VM {
	return &VM{ID: vmID, client: c, baseURL: c.baseURL}
}

// newVM binds a decoded VMInfo to the endpoint that serves it. A VM listed from
// the control plane carries its own placement, which is the only way a handle
// from ListVMs can address a machine in another region.
func (c *Client) newVM(info *VMInfo, fallback string) *VM {
	base := fallback
	if info.Provider != "" && info.Region != "" {
		base = computeBaseURL(info.Provider, info.Region)
	}
	return &VM{ID: info.VMID, Info: info, client: c, baseURL: base}
}

// Fork creates a VM from a source.
//
// Every fork carries an Idempotency-Key because the retry it guards against is
// the SDK's own: a 502/504 is a response, so it is retried even on a mutation,
// and the origin may already have built the VM.
func (c *Client) Fork(ctx context.Context, req ForkRequest) (*VM, error) {
	if strings.TrimSpace(req.Dockerfile) != "" {
		return nil, errors.New("arker: Dockerfile forks are not implemented in the Go SDK; fork the base image and apply steps with VM.Run, or use the Python or TypeScript SDK")
	}
	key := req.IdempotencyKey
	if strings.TrimSpace(key) == "" {
		key = newIdempotencyKey("sdk-fork-")
	}
	base, err := c.BaseURL()
	if err != nil {
		return nil, err
	}
	var info VMInfo
	if _, err := c.do(ctx, call{
		method: http.MethodPost, path: "/v1/fork", base: base, body: req, key: key, out: &info,
	}); err != nil {
		return nil, err
	}
	if strings.TrimSpace(info.VMID) == "" {
		return nil, errors.New("arker: fork returned no vm_id")
	}
	return c.newVM(&info, base), nil
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

// ListVMsOptions narrows a listing. OrgID and Public are admin filters.
type ListVMsOptions struct {
	Cursor   string
	Limit    int
	Region   string
	Provider string
	OrgID    string
	State    string
	Public   *bool
}

// VMList is one page of VMs.
type VMList struct {
	VMs        []*VM
	NextCursor string
}

// ListVMs pages the org's VMs. It goes through the CONTROL PLANE so it can
// aggregate across providers and regions, and so it works without placement.
func (c *Client) ListVMs(ctx context.Context, opts ListVMsOptions) (*VMList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	q.str("region", opts.Region)
	q.str("provider", opts.Provider)
	q.str("org_id", opts.OrgID)
	q.str("state", opts.State)
	q.boolPtr("public", opts.Public)
	var out struct {
		VMs        []VMInfo `json:"vms"`
		NextCursor string   `json:"next_cursor"`
	}
	if _, err := c.control(ctx, q.on("/v1/vms"), &out); err != nil {
		return nil, err
	}
	list := &VMList{VMs: make([]*VM, 0, len(out.VMs)), NextCursor: out.NextCursor}
	for i := range out.VMs {
		list.VMs = append(list.VMs, c.newVM(&out.VMs[i], c.baseURL))
	}
	return list, nil
}

// Refresh re-reads this VM into Info.
func (v *VM) Refresh(ctx context.Context) error {
	var info VMInfo
	if _, err := v.do(ctx, http.MethodGet, v.path(""), nil, &info); err != nil {
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

// UpdateRequest patches a VM. Every field is optional; omitted fields are left
// unchanged. An empty, non-nil SSHPublicKeys removes all authorized keys, and a
// non-nil Policies replaces the network policy wholesale.
type UpdateRequest struct {
	Description   *string    `json:"description,omitempty"`
	Resources     *Resources `json:"resources,omitempty"`
	SSHPublicKeys []string   `json:"ssh_public_keys,omitempty"`
	Policies      *PolicyDoc `json:"policies,omitempty"`

	// ClearDescription sends an explicit null, which is how the API is told to
	// clear the field rather than leave it alone.
	ClearDescription bool `json:"-"`
}

// Update changes this VM's description, resources, authorized SSH keys and/or
// network policy, and returns the updated record.
func (v *VM) Update(ctx context.Context, req UpdateRequest) (*VMInfo, error) {
	body := any(req)
	if req.ClearDescription {
		shadow := struct {
			UpdateRequest
			Description *string `json:"description"`
		}{UpdateRequest: req}
		shadow.UpdateRequest.Description = nil
		body = shadow
	}
	var info VMInfo
	if _, err := v.do(ctx, http.MethodPatch, v.path(""), body, &info); err != nil {
		return nil, err
	}
	v.Info = &info
	return &info, nil
}

// Delete removes this VM. Retry-safe: an already-absent VM is success.
func (v *VM) Delete(ctx context.Context) error {
	status, err := v.do(ctx, http.MethodDelete, v.path(""), nil, nil)
	if status == http.StatusNotFound {
		return nil
	}
	return err
}

// query builds a URL query from optional values, dropping the unset ones.
type query struct{ values url.Values }

func newQuery() *query { return &query{url.Values{}} }

func (q *query) str(key, value string) {
	if value != "" {
		q.values.Set(key, value)
	}
}

func (q *query) num(key string, value int) {
	if value != 0 {
		q.values.Set(key, strconv.Itoa(value))
	}
}

func (q *query) numPtr(key string, value *int) {
	if value != nil {
		q.values.Set(key, strconv.Itoa(*value))
	}
}

func (q *query) boolPtr(key string, value *bool) {
	if value != nil {
		q.values.Set(key, strconv.FormatBool(*value))
	}
}

func (q *query) csv(key string, values []string) {
	if len(values) > 0 {
		q.values.Set(key, strings.Join(values, ","))
	}
}

func (q *query) on(path string) string {
	if encoded := q.values.Encode(); encoded != "" {
		return path + "?" + encoded
	}
	return path
}
