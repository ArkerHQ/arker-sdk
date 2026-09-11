package unit

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

func TestMountRequestsPreserveIdentityStatusAndPagination(t *testing.T) {
	var calls []string
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		switch r.Method {
		case http.MethodPost:
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if !reflect.DeepEqual(body, map[string]string{"filesystem_id": "fs_1", "path": "/mnt/data"}) {
				t.Errorf("create body: %v", body)
			}
			fmt.Fprint(w, `{"mount_id":"01EXISTING","vm_id":"vm_1","filesystem_id":"fs_1","path":"/mnt/data","status":"attaching"}`)
		case http.MethodGet:
			query := r.URL.Query()
			if query.Get("filesystem_id") != "fs_1" || query.Get("cursor") != "page" || query.Get("limit") != "1" || len(query) != 3 {
				t.Errorf("list query: %v", query)
			}
			fmt.Fprint(w, `{"mounts":[{"mount_id":"01EXISTING","vm_id":"vm_1","filesystem_id":"fs_1","path":"/mnt/data","status":"failed","status_detail":"mount refused"}],"next_cursor":"next"}`)
		case http.MethodDelete:
			fmt.Fprint(w, `{"deleted":true}`)
		default:
			t.Errorf("unexpected method: %s", r.Method)
		}
	}, reject(t, "control"))
	vm := c.VM("vm_1")
	ctx := context.Background()
	mount, err := vm.CreateMount(ctx, "fs_1", "/mnt/data")
	if err != nil {
		t.Fatal(err)
	}
	if mount.MountID != "01EXISTING" || mount.Status != "attaching" {
		t.Fatalf("created mount: %+v", mount)
	}
	list, err := vm.ListMounts(ctx, arker.ListMountsOptions{FilesystemID: "fs_1", Cursor: "page", Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(list.Mounts) != 1 || list.NextCursor != "next" {
		t.Fatalf("mount page: %+v", list)
	}
	if got := list.Mounts[0]; got.MountID != mount.MountID || got.Status != "failed" || got.StatusDetail != "mount refused" {
		t.Fatalf("listed mount: %+v", got)
	}
	if err := vm.DeleteMount(ctx, mount.MountID); err != nil {
		t.Fatal(err)
	}
	want := []string{"POST /v1/vms/vm_1/mounts", "GET /v1/vms/vm_1/mounts", "DELETE /v1/vms/vm_1/mounts/01EXISTING"}
	if !reflect.DeepEqual(calls, want) {
		t.Fatalf("requests: %v; want %v", calls, want)
	}
}
