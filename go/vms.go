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

// GPUResourceBand is the allowed range for one GPU resource.
type GPUResourceBand struct {
	Min     int `json:"min"`
	Max     int `json:"max"`
	Default int `json:"default"`
}

// PlatformGPULimits is the GPU sizing offered on one compatible platform.
type PlatformGPULimits struct {
	Name    string           `json:"name,omitempty"`
	VRAMMiB *GPUResourceBand `json:"vram_mib,omitempty"`
	SMs     *GPUResourceBand `json:"sms,omitempty"`
}

// CompatiblePlatform is one platform a source can be forked onto, with the
// limits that apply there. When an entry carries bounds they WIN over the flat
// Min*/Max* fields on VMInfo, which are a single-platform projection of the
// same data.
type CompatiblePlatform struct {
	ID               string             `json:"id"`
	DisplayName      string             `json:"display_name"`
	Architecture     string             `json:"architecture"`
	MinVCPUs         *int               `json:"min_vcpus,omitempty"`
	MaxVCPUs         *int               `json:"max_vcpus,omitempty"`
	MinMemoryMiB     *int               `json:"min_memory_mib,omitempty"`
	MaxMemoryMiB     *int               `json:"max_memory_mib,omitempty"`
	MinDiskMiB       *int               `json:"min_disk_mib,omitempty"`
	MaxDiskMiB       *int               `json:"max_disk_mib,omitempty"`
	DefaultVCPUs     *int               `json:"default_vcpus,omitempty"`
	DefaultMemoryMiB *int               `json:"default_memory_mib,omitempty"`
	DefaultDiskMiB   *int               `json:"default_disk_mib,omitempty"`
	GPU              *PlatformGPULimits `json:"gpu,omitempty"`
}

// GPUPlatformLimits is the GPU sizing for one platform this VM can fork onto.
type GPUPlatformLimits struct {
	Platform string           `json:"platform"`
	GPU      string           `json:"gpu,omitempty"`
	VRAMMiB  *GPUResourceBand `json:"vram_mib,omitempty"`
	SMs      *GPUResourceBand `json:"sms,omitempty"`
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

	// IdempotencyKey is used verbatim. The only form that survives across
	// processes, so it is what makes YOUR retry after an UnknownOutcomeError
	// converge instead of building a second machine. Wins over Idempotency.
	//
	// Idempotency generates a key for this call instead. Bound once, before the
	// retry loop, so every attempt of a single fork presents the same one --
	// which is what makes the SDK's OWN retry safe, since a 502/504 is a
	// *response* and is retried even on a mutation.
	//
	// Neither set is the default: the fork sends no key and is never
	// deduplicated, matching the API. Both are headers, never body fields --
	// unknown body fields are rejected.
	IdempotencyKey string `json:"-"`
	Idempotency    bool   `json:"-"`
}

// VMInfo is a virtual machine as the API reports it, mirroring the contract Vm.
type VMInfo struct {
	VMID                string               `json:"vm_id"`
	Name                string               `json:"name,omitempty"`
	Description         string               `json:"description,omitempty"`
	OwnerOrgID          string               `json:"owner_org_id,omitempty"`
	Hostname            string               `json:"hostname,omitempty"`
	CreatedAt           string               `json:"created_at,omitempty"`
	LastActiveAt        string               `json:"last_active_at,omitempty"`
	Public              bool                 `json:"public,omitempty"`
	Region              string               `json:"region,omitempty"`
	Provider            string               `json:"provider,omitempty"`
	Platform            string               `json:"platform,omitempty"`
	RootSourceVMID      string               `json:"root_source_vm_id,omitempty"`
	RootSourceVMName    string               `json:"root_source_vm_name,omitempty"`
	CompatiblePlatforms []CompatiblePlatform `json:"compatible_platforms,omitempty"`
	GPUPlatforms        []GPUPlatformLimits  `json:"gpu_platforms,omitempty"`
	MaxVCPUs            *int                 `json:"max_vcpus,omitempty"`
	MinVCPUs            *int                 `json:"min_vcpus,omitempty"`
	MaxMemoryMiB        *int                 `json:"max_memory_mib,omitempty"`
	MinMemoryMiB        *int                 `json:"min_memory_mib,omitempty"`
	MaxDiskMiB          *int                 `json:"max_disk_mib,omitempty"`
	MinDiskMiB          *int                 `json:"min_disk_mib,omitempty"`
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

// newVM binds a decoded VMInfo to base VERBATIM.
//
// It deliberately does not derive an endpoint from the VM's provider/region.
// Fork and GetVM already went to the endpoint the caller configured, and that
// endpoint is what serves the machine; recomputing a public URL from the
// response would send every later per-VM call for a feature env or a private
// deployment to production instead. Only ListVMs, which aggregates across
// regions through the control plane, needs a per-VM endpoint -- and it derives
// one itself.
func (c *Client) newVM(info *VMInfo, base string) *VM {
	return &VM{ID: info.VMID, Info: info, client: c, baseURL: base}
}

// Fork creates a VM from a source.
//
// Idempotency is opt-in: see ForkRequest.IdempotencyKey and .Idempotency. A
// fork with neither sends no key and is never deduplicated, which is the API's
// own behaviour.
func (c *Client) Fork(ctx context.Context, req ForkRequest) (*VM, error) {
	if strings.TrimSpace(req.Dockerfile) != "" {
		return nil, errors.New("arker: Dockerfile forks are not implemented in the Go SDK; fork the base image and apply steps with VM.Run, or use the Python or TypeScript SDK")
	}
	key := forkIdempotencyKey(req)
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

// forkIdempotencyKey resolves the header for one fork, or "" to send none.
// An explicit key wins over the flag: it is the more specific instruction, and
// the only one the caller can act on later.
func forkIdempotencyKey(req ForkRequest) string {
	if key := strings.TrimSpace(req.IdempotencyKey); key != "" {
		return key
	}
	if req.Idempotency {
		return newIdempotencyKey("sdk-fork-")
	}
	return ""
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
		// This listing spans regions, so a placed VM is addressed at its own
		// regional endpoint rather than the client's.
		base := c.baseURL
		if info := &out.VMs[i]; info.Provider != "" && info.Region != "" {
			base = computeBaseURL(info.Provider, info.Region)
		}
		list.VMs = append(list.VMs, c.newVM(&out.VMs[i], base))
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

// UpdateRequest patches a VM. Every field is optional and a nil one is left
// unchanged.
//
// The pointers are what make "clear it" expressible. A plain value with
// omitempty cannot: the empty string and empty slice the API reads as "clear
// this" are exactly the values encoding/json drops.
//
//	Description:   arker.Ptr("")   // clears it
//	SSHPublicKeys: &[]string{}     // removes every authorized key
//
// Use the empty string, NOT a JSON null, to clear the description. openapi
// documents null as equivalent, but a null is silently ignored -- verified
// against a live deployment: PATCH {"description":null} left the value intact
// while {"description":""} cleared it.
type UpdateRequest struct {
	Description   *string    `json:"description,omitempty"`
	Resources     *Resources `json:"resources,omitempty"`
	SSHPublicKeys *[]string  `json:"ssh_public_keys,omitempty"`
	Policies      *PolicyDoc `json:"policies,omitempty"`
}

// Update changes this VM's description, resources, authorized SSH keys and/or
// network policy, and returns the updated record.
func (v *VM) Update(ctx context.Context, req UpdateRequest) (*VMInfo, error) {
	var info VMInfo
	if _, err := v.do(ctx, http.MethodPatch, v.path(""), req, &info); err != nil {
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
