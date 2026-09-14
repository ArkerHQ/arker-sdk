package unit

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
)

func TestWriteFileUsesSupportedSyncChunks(t *testing.T) {
	for _, size := range []int{0, 5, 20*1024*1024 + 1} {
		t.Run(fmt.Sprint(size), func(t *testing.T) {
			data := bytes.Repeat([]byte{255}, size)
			var received []byte
			ids := map[string]bool{}
			c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "POST" || r.URL.Path != "/v1/vms/vm_1/sync" {
					t.Errorf("unexpected route: %s %s", r.Method, r.URL.Path)
					http.NotFound(w, r)
					return
				}
				var body struct {
					Op     string
					Writes []struct {
						Path     string
						Size     int
						Start    int
						End      int
						Content  string
						UploadID string `json:"upload_id"`
					}
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body.Op != "write" {
					t.Errorf("op=%s", body.Op)
				}
				results := []map[string]any{}
				for _, entry := range body.Writes {
					chunk, err := base64.StdEncoding.DecodeString(entry.Content)
					if err != nil {
						t.Error(err)
					}
					if len(chunk) > 5*1024*1024 || entry.Size != size || entry.Start != len(received) || entry.Path != "/tmp/probe" {
						t.Errorf("invalid chunk metadata")
					}
					received = append(received, chunk...)
					ids[entry.UploadID] = true
					if entry.End != len(received) {
						t.Errorf("invalid chunk end")
					}
					results = append(results, map[string]any{"path": entry.Path, "size": size, "complete": len(received) == size, "written": len(received) == size})
				}
				json.NewEncoder(w).Encode(map[string]any{"results": results, "ok": true})
			}, reject(t, "control"))
			if err := c.VM("vm_1").WriteFile(context.Background(), "/tmp/probe", data); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(received, data) || len(ids) != 1 {
				t.Fatal("upload bytes or identity differ")
			}
		})
	}
}

func TestWriteFileRejectsUnfinishedFile(t *testing.T) {
	for _, result := range []string{`{"complete":false,"written":false}`, `{"complete":true,"written":false}`} {
		t.Run(result, func(t *testing.T) {
			c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, `{"ok":true,"op":"write","results":[%s]}`, result)
			}, reject(t, "control"))
			if err := c.VM("vm_1").WriteFile(context.Background(), "/tmp/x", []byte("x")); err == nil {
				t.Fatal("unfinished write reported success")
			}
		})
	}
}
