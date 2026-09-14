package e2e

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"fmt"
	"strings"
	"testing"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

func TestFileWriteChunkBoundaries(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)
	for _, size := range []int{0, 5, 4*1024*1024 + 1, 20 * 1024 * 1024} {
		t.Run(fmt.Sprint(size), func(t *testing.T) {
			payload := make([]byte, size)
			if _, err := rand.Read(payload); err != nil {
				t.Fatal(err)
			}
			path := fmt.Sprintf("/tmp/sdk-live-%d.bin", size)
			if err := vm.WriteFile(ctx, path, payload); err != nil {
				t.Fatalf("write: %v", err)
			}
			got, err := vm.ReadFile(ctx, path)
			if err != nil {
				t.Fatalf("read: %v", err)
			}
			if !bytes.Equal(got, payload) {
				t.Fatalf("readback differs for %d bytes", size)
			}
			expected := fmt.Sprintf("%x", sha256.Sum256(payload))
			out, err := vm.Run(ctx, arker.RunRequest{Command: "sha256sum " + path + " | cut -d' ' -f1"})
			if err != nil {
				t.Fatalf("guest hash: %v", err)
			}
			if out.ExitCode == nil || *out.ExitCode != 0 || strings.TrimSpace(out.Stdout) != expected {
				t.Fatalf("guest hash differs: exit=%v stdout=%q stderr=%q", out.ExitCode, out.Stdout, out.Stderr)
			}
		})
	}
}
