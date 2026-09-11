package arker

import (
	"context"
	"net/http"
)

// Filesystem is a standalone, org-scoped volume that can be bound into VMs.
type Filesystem struct {
	FilesystemID string `json:"filesystem_id"`
	Name         string `json:"name"`
	OwnerOrgID   string `json:"owner_org_id,omitempty"`
	CreatedAt    string `json:"created_at,omitempty"`
	SizeBytes    *int64 `json:"size_bytes,omitempty"`
	Region       string `json:"region,omitempty"`
	Provider     string `json:"provider,omitempty"`
}

// FilesystemList is one page of filesystems.
type FilesystemList struct {
	Filesystems []Filesystem `json:"filesystems"`
	NextCursor  string       `json:"next_cursor,omitempty"`
}

// ListFilesystemsOptions narrows a filesystem listing.
type ListFilesystemsOptions struct {
	Cursor     string
	Limit      int
	NamePrefix string
}

// ListFilesystems pages the org's filesystems.
//
// Regional, not control-plane: the control-plane host does not route
// /v1/filesystems, while the regional endpoint serves it.
func (c *Client) ListFilesystems(ctx context.Context, opts ListFilesystemsOptions) (*FilesystemList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	q.str("name_prefix", opts.NamePrefix)
	var out FilesystemList
	_, err := c.get(ctx, "", q.on("/v1/filesystems"), &out)
	return &out, err
}

// CreateFilesystem creates a filesystem.
func (c *Client) CreateFilesystem(ctx context.Context, name string) (*Filesystem, error) {
	var out Filesystem
	_, err := c.do(ctx, call{
		method: http.MethodPost, path: "/v1/filesystems",
		body: map[string]string{"name": name}, out: &out,
	})
	return &out, err
}

// GetFilesystem fetches one filesystem.
func (c *Client) GetFilesystem(ctx context.Context, filesystemID string) (*Filesystem, error) {
	var out Filesystem
	_, err := c.get(ctx, "", "/v1/filesystems/"+segment(filesystemID), &out)
	return &out, err
}

// DeleteFilesystem removes a filesystem. Retry-safe: an already-absent
// filesystem is success.
func (c *Client) DeleteFilesystem(ctx context.Context, filesystemID string) error {
	status, err := c.do(ctx, call{
		method: http.MethodDelete, path: "/v1/filesystems/" + segment(filesystemID),
	})
	if status == http.StatusNotFound {
		return nil
	}
	return err
}

// Mount is a filesystem bound into a VM at a path.
type Mount struct {
	MountID      string `json:"mount_id"`
	VMID         string `json:"vm_id,omitempty"`
	FilesystemID string `json:"filesystem_id"`
	Path         string `json:"path"`
	Region       string `json:"region,omitempty"`
	Status       string `json:"status,omitempty"`
	StatusDetail string `json:"status_detail,omitempty"`
}

// MountList is one page of a VM's filesystem bindings.
type MountList struct {
	Mounts     []Mount `json:"mounts"`
	NextCursor string  `json:"next_cursor,omitempty"`
}

// ListMountsOptions narrows a mount listing.
type ListMountsOptions struct {
	Cursor       string
	Limit        int
	FilesystemID string
}

// ListMounts pages the filesystems bound into this VM.
func (v *VM) ListMounts(ctx context.Context, opts ListMountsOptions) (*MountList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	q.str("filesystem_id", opts.FilesystemID)
	var out MountList
	_, err := v.do(ctx, http.MethodGet, q.on(v.path("/mounts")), nil, &out)
	return &out, err
}

// CreateMount binds a filesystem into this VM at path.
func (v *VM) CreateMount(ctx context.Context, filesystemID, path string) (*Mount, error) {
	body := map[string]string{"filesystem_id": filesystemID}
	if path != "" {
		body["path"] = path
	}
	var out Mount
	_, err := v.do(ctx, http.MethodPost, v.path("/mounts"), body, &out)
	return &out, err
}

// DeleteMount unbinds a filesystem. Retry-safe: an already-absent binding is
// success.
func (v *VM) DeleteMount(ctx context.Context, mountID string) error {
	status, err := v.do(ctx, http.MethodDelete, v.path("/mounts/"+segment(mountID)), nil, nil)
	if status == http.StatusNotFound {
		return nil
	}
	return err
}
