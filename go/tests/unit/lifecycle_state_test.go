package unit

import (
	"encoding/json"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

func TestMountLifecycleState(t *testing.T) {
	for _, state := range []string{"attaching", "mounted", "failed"} {
		t.Run(state, func(t *testing.T) {
			detail := ""
			if state == "failed" {
				detail = "internal server error"
			}
			payload, err := json.Marshal(map[string]string{
				"mount_id": "mount-1", "vm_id": "vm-1", "filesystem_id": "fs-1",
				"path": "/mnt/shared", "state": state, "state_detail": detail,
			})
			if err != nil {
				t.Fatal(err)
			}
			var mount arker.Mount
			if err := json.Unmarshal(payload, &mount); err != nil {
				t.Fatal(err)
			}
			if mount.State != state || mount.StateDetail != detail {
				t.Fatalf("mount lost lifecycle fields: %+v", mount)
			}
		})
	}
}

func TestPoolWorkerLifecycleState(t *testing.T) {
	var worker arker.PoolWorker
	if err := json.Unmarshal([]byte(`{"worker_id":"worker-1","state":"running"}`), &worker); err != nil {
		t.Fatal(err)
	}
	if worker.State != "running" {
		t.Fatalf("worker lost lifecycle state: %+v", worker)
	}
}
