package arker_test

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

// TestLiveSmoke exercises the SDK against a real Arker deployment.
//
//	ARKER_API_KEY=ark_... ARKER_BASE_URL=http://<env>/api \
//	  go test -run LiveSmoke -v ./...
//
// Deliberately NO default base URL. This creates and deletes real VMs, and
// defaulting to production means anyone with ARKER_API_KEY exported for some
// other reason silently runs it against prod.
func TestLiveSmoke(t *testing.T) {
	key := strings.TrimSpace(os.Getenv("ARKER_API_KEY"))
	base := strings.TrimSpace(os.Getenv("ARKER_BASE_URL"))
	if key == "" || base == "" {
		t.Skip("ARKER_API_KEY and ARKER_BASE_URL must both be set; this creates real VMs")
	}
	golden := strings.TrimSpace(os.Getenv("ARKER_TEST_GOLDEN"))
	if golden == "" {
		golden = "ubuntu-base"
	}

	client, err := arker.New(arker.Options{APIKey: key, BaseURL: base})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()

	var made []*arker.VM
	defer func() {
		for _, vm := range made {
			cleanup, done := context.WithTimeout(context.Background(), 3*time.Minute)
			if err := vm.Delete(cleanup); err != nil {
				t.Errorf("cleanup %s: %v", vm.ID, err)
			}
			done()
		}
	}()

	// ── fork ────────────────────────────────────────────────────────────
	vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: golden})
	if err != nil {
		t.Fatalf("fork: %v", err)
	}
	made = append(made, vm)
	if vm.ID == "" || vm.Info == nil {
		t.Fatalf("fork returned an unpopulated handle: %+v", vm)
	}
	t.Logf("forked %s (state=%s)", vm.ID, vm.Info.State)

	// ── the guarantee: same key replays, never a second machine ─────────
	idem := "sdk-live-" + strings.ReplaceAll(time.Now().UTC().Format("150405.000"), ".", "")
	first, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: golden, IdempotencyKey: idem})
	if err != nil {
		t.Fatalf("keyed fork: %v", err)
	}
	made = append(made, first)
	replay, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: golden, IdempotencyKey: idem})
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if replay.ID != first.ID {
		made = append(made, replay)
		t.Fatalf("the key built a SECOND machine: %s then %s", first.ID, replay.ID)
	}
	t.Logf("replay converged on %s", replay.ID)

	// Reusing that key for a DIFFERENT request must conflict, not fork.
	_, err = client.Fork(ctx, arker.ForkRequest{
		SourceVMName: golden, IdempotencyKey: idem, Description: "a different fork",
	})
	if !arker.IsConflict(err) {
		t.Fatalf("want a 409 for a reused key on a different request, got %v", err)
	}

	// ── run ─────────────────────────────────────────────────────────────
	out, err := vm.Run(ctx, arker.RunRequest{Command: "echo sdk-live-ok"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if out.ExitCode == nil || *out.ExitCode != 0 {
		t.Fatalf("exit code %v, stderr=%q", out.ExitCode, out.Stderr)
	}
	if !strings.Contains(out.Stdout, "sdk-live-ok") {
		t.Fatalf("stdout was %q", out.Stdout)
	}

	// ── sessions, refresh, get ──────────────────────────────────────────
	sessions, err := vm.ListSessions(ctx)
	if err != nil {
		t.Fatalf("list sessions: %v", err)
	}
	t.Logf("sessions: %d", len(sessions))

	if err := vm.Refresh(ctx); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	fetched, found, err := client.GetVM(ctx, vm.ID)
	if err != nil || !found || fetched.ID != vm.ID {
		t.Fatalf("get: found=%v id=%v err=%v", found, fetched, err)
	}

	// ── delete is retry-safe, and a gone VM reads as absent ─────────────
	if err := vm.Delete(ctx); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if err := vm.Delete(ctx); err != nil {
		t.Fatalf("second delete must be a no-op, got: %v", err)
	}
	if _, found, err := client.GetVM(ctx, vm.ID); err != nil || found {
		t.Fatalf("deleted VM still present: found=%v err=%v", found, err)
	}
	made = made[1:] // already deleted
	t.Log("live smoke ok")
}
