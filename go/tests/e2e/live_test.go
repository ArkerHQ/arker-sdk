// Package e2e exercises the SDK against a real Arker deployment.
//
//	ARKER_API_KEY=ark_... ARKER_BASE_URL=https://<env>/api go test -v ./tests/e2e/
//
// Deliberately NO default base URL. These tests create and delete real VMs, and
// defaulting to production means anyone with ARKER_API_KEY exported for some
// other reason silently runs them against prod.
package e2e

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

const (
	forkBudget    = 10 * time.Minute
	cleanupBudget = 3 * time.Minute
)

type harness struct {
	client *arker.Client
	golden string

	mu    sync.Mutex
	owned []*arker.VM
}

func setup(t *testing.T) *harness {
	t.Helper()
	key := strings.TrimSpace(os.Getenv("ARKER_API_KEY"))
	base := strings.TrimSpace(os.Getenv("ARKER_BASE_URL"))
	if key == "" || base == "" {
		t.Skip("ARKER_API_KEY and ARKER_BASE_URL must both be set; these tests create real VMs")
	}
	client, err := arker.New(arker.Options{APIKey: key, BaseURL: base})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	golden := strings.TrimSpace(os.Getenv("ARKER_TEST_GOLDEN"))
	if golden == "" {
		golden = "ubuntu-base"
	}
	h := &harness{client: client, golden: golden}
	// Cleanup runs after failures, timeouts and interrupts, on a context of its
	// own so a blown test deadline cannot orphan a billable machine.
	t.Cleanup(func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		for _, vm := range h.owned {
			ctx, done := context.WithTimeout(context.Background(), cleanupBudget)
			if err := vm.Delete(ctx); err != nil {
				t.Errorf("cleanup %s: %v", vm.ID, err)
			}
			done()
		}
	})
	return h
}

func (h *harness) own(vm *arker.VM) *arker.VM {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.owned = append(h.owned, vm)
	return vm
}

func (h *harness) disown(id string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.owned = slices.DeleteFunc(h.owned, func(v *arker.VM) bool { return v.ID == id })
}

func (h *harness) fork(t *testing.T, ctx context.Context) *arker.VM {
	t.Helper()
	vm, err := h.client.Fork(ctx, arker.ForkRequest{SourceVMName: h.golden})
	if err != nil {
		t.Fatalf("fork: %v", err)
	}
	if vm.ID == "" || vm.Info == nil {
		t.Fatalf("fork returned an unpopulated handle: %+v", vm)
	}
	return h.own(vm)
}

func budget(t *testing.T, d time.Duration) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	t.Cleanup(cancel)
	return ctx
}

func unique(prefix string) string {
	return fmt.Sprintf("%s-%d", prefix, time.Now().UnixNano())
}

// ── Org-wide surface ────────────────────────────────────────────────────

func TestOrgSurface(t *testing.T) {
	h := setup(t)
	ctx := budget(t, 2*time.Minute)

	who, err := h.client.Whoami(ctx)
	if err != nil {
		t.Fatalf("whoami: %v", err)
	}
	if who.OrgID == "" {
		t.Fatal("whoami returned no org_id")
	}
	t.Logf("org %s (%s)", who.OrgID, who.OrgName)

	regions, err := h.client.ListRegions(ctx)
	if err != nil {
		t.Fatalf("list regions: %v", err)
	}
	for _, r := range regions {
		if r.Provider == "" || r.Region == "" || r.Endpoint == "" {
			t.Fatalf("incomplete placement: %+v", r)
		}
	}
	t.Logf("%d placements", len(regions))

	// Asserted on the DECODED page, not just the status: a wrong envelope field
	// returns an empty list forever and every err-only check stays green.
	activity, err := h.client.ListRuns(ctx, arker.ListOrgRunsOptions{Limit: 5})
	if err != nil {
		t.Fatalf("list org runs: %v", err)
	}
	if activity.Limit != 5 {
		t.Fatalf("the server echoed limit %d, want the 5 that was asked for", activity.Limit)
	}
	// A canary has to be a field the platform fills on EVERY row, or this suite
	// fails on a platform gap rather than on an SDK decode bug. Measured against
	// prod over a 100-row page: `request_id` is empty on 100/100 despite being
	// required by openapi.json, and `run_id`/`session_id`/`vm_id` are blank on
	// fork rows, which are not runs. `t_ms` and `path` are the two that survive
	// both endpoints, and a wrong envelope field would leave both zero.
	// Without this the loop below is skipped on an empty page and the test
	// passes having checked nothing -- which is the very failure the envelope
	// canary exists to catch.
	if len(activity.Rows) == 0 {
		t.Fatal("activity decoded zero rows, so the canary below never ran")
	}
	for _, row := range activity.Rows {
		if row.TMs == 0 || row.Path == "" {
			// Not %+v: a row carries BodyIn/BodyOut, which is customer command
			// text, and a failing test must not print it.
			t.Fatalf("an activity row decoded empty: t_ms=%d path=%q endpoint=%q",
				row.TMs, row.Path, row.Endpoint)
		}
	}
	t.Logf("%d activity rows", len(activity.Rows))
}

func TestDiscoverRegionsNeedsNoCredentials(t *testing.T) {
	control := strings.TrimSpace(os.Getenv("ARKER_CONTROL_BASE_URL"))
	if control == "" {
		t.Skip("ARKER_CONTROL_BASE_URL must be set to probe the unauthenticated catalog")
	}
	regions, err := arker.DiscoverRegions(budget(t, time.Minute), control)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	t.Logf("%d public placements", len(regions))
}

// ── Fork, idempotency, lifecycle ────────────────────────────────────────

func TestForkIsIdempotentUnderOneKey(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	key := unique("sdk-live-idem")

	first, err := h.client.Fork(ctx, arker.ForkRequest{SourceVMName: h.golden, IdempotencyKey: key})
	if err != nil {
		t.Fatalf("keyed fork: %v", err)
	}
	h.own(first)

	replay, err := h.client.Fork(ctx, arker.ForkRequest{SourceVMName: h.golden, IdempotencyKey: key})
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if replay.ID != first.ID {
		h.own(replay)
		t.Fatalf("one key built a SECOND machine: %s then %s", first.ID, replay.ID)
	}

	// The same key naming a DIFFERENT request must conflict, not fork. Owned
	// first: if it ever forks, that machine is real and billable, and this is
	// the path that would leak it.
	stray, err := h.client.Fork(ctx, arker.ForkRequest{
		SourceVMName: h.golden, IdempotencyKey: key, Description: "a different fork",
	})
	if stray != nil {
		h.own(stray)
	}
	if !arker.IsConflict(err) {
		t.Fatalf("want a 409 for a reused key on a different request, got %v", err)
	}
}

func TestVMLifecycle(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	if err := vm.Refresh(ctx); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if vm.Info.OwnerOrgID == "" || vm.Info.State == "" {
		t.Fatalf("refresh left the record incomplete: %+v", vm.Info)
	}

	fetched, found, err := h.client.GetVM(ctx, vm.ID)
	if err != nil || !found || fetched.ID != vm.ID {
		t.Fatalf("get: found=%v err=%v", found, err)
	}

	list, err := h.client.ListVMs(ctx, arker.ListVMsOptions{Limit: 50})
	if err != nil {
		t.Fatalf("list vms: %v", err)
	}
	if !slices.ContainsFunc(list.VMs, func(v *arker.VM) bool { return v.ID == vm.ID }) {
		t.Fatalf("a live VM (%s) is missing from a %d-row listing", vm.ID, len(list.VMs))
	}

	description := unique("sdk-live")
	updated, err := vm.Update(ctx, arker.UpdateRequest{Description: &description})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if updated.Description != description {
		t.Fatalf("description is %q, want %q", updated.Description, description)
	}
	cleared, err := vm.Update(ctx, arker.UpdateRequest{Description: arker.Ptr("")})
	if err != nil {
		t.Fatalf("clear description: %v", err)
	}
	if cleared.Description != "" {
		t.Fatalf("description survived an explicit clear: %q", cleared.Description)
	}

	// Delete is retry-safe, and a gone VM reads as absent.
	if err := vm.Delete(ctx); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if err := vm.Delete(ctx); err != nil {
		t.Fatalf("second delete must be a no-op, got: %v", err)
	}
	h.disown(vm.ID)
	if _, found, err := h.client.GetVM(ctx, vm.ID); err != nil || found {
		t.Fatalf("deleted VM still present: found=%v err=%v", found, err)
	}
}

func TestForkFromALiveVM(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	parent := h.fork(t, ctx)

	marker := unique("child-sees")
	if _, err := parent.Run(ctx, arker.RunRequest{
		Command: fmt.Sprintf("echo %s > /tmp/marker", marker),
	}); err != nil {
		t.Fatalf("seed parent: %v", err)
	}

	child, err := parent.Fork(ctx, arker.ForkRequest{})
	if err != nil {
		t.Fatalf("fork child: %v", err)
	}
	h.own(child)
	if child.ID == parent.ID {
		t.Fatal("fork returned the parent")
	}

	out, err := child.Run(ctx, arker.RunRequest{Command: "cat /tmp/marker"})
	if err != nil {
		t.Fatalf("child run: %v", err)
	}
	if !strings.Contains(out.Stdout, marker) {
		t.Fatalf("the child did not inherit the parent's disk: %q", out.Stdout)
	}
}

// ── Runs ────────────────────────────────────────────────────────────────

func TestRuns(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

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
	if out.RunID == "" || out.SessionID == "" {
		t.Fatalf("run lost its identifiers: %+v", out)
	}

	// A nonzero exit is a normal result, not a transport error.
	failed, err := vm.Run(ctx, arker.RunRequest{Command: "exit 7"})
	if err != nil {
		t.Fatalf("a failing command must not error the call: %v", err)
	}
	if failed.ExitCode == nil || *failed.ExitCode != 7 {
		t.Fatalf("exit code %v, want 7", failed.ExitCode)
	}

	record, err := vm.GetRun(ctx, out.RunID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	if record.RunID != out.RunID || !strings.Contains(record.Stdout, "sdk-live-ok") {
		t.Fatalf("fetched run does not match: %+v", record)
	}

	runs, err := vm.ListRuns(ctx, arker.ListRunsOptions{Limit: 20})
	if err != nil {
		t.Fatalf("list runs: %v", err)
	}
	if !slices.ContainsFunc(runs.Runs, func(r arker.RunSummary) bool { return r.RunID == out.RunID }) {
		t.Fatalf("run %s missing from a %d-row listing", out.RunID, len(runs.Runs))
	}
}

func TestBackgroundRunAndCancel(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	// Its own session: a run entering a session that holds a live foreground
	// process interrupts it.
	session, err := vm.CreateSession(ctx, arker.CreateSessionRequest{})
	if err != nil {
		t.Fatalf("create session: %v", err)
	}
	started, err := vm.Run(ctx, arker.RunRequest{
		Command: "sleep 300", SessionID: session.SessionID, TimeToBackground: arker.Ptr(0),
	})
	if err != nil {
		t.Fatalf("background run: %v", err)
	}
	if started.Completed() {
		t.Fatalf("TimeToBackground=0 waited for the run: %+v", started)
	}
	if started.RunID == "" {
		t.Fatal("background ack carried no run_id, so the run is unreachable")
	}

	// PLATFORM RACE, not an SDK behaviour: the API acks a backgrounded run with
	// 202 and state "running" BEFORE the guest has spawned the process, and a
	// cancel inside that window fails 404 "signal_exec: no running process"
	// while the run carries on running. Measured against prod over raw HTTP at
	// delays 0/1/3/10s: only 0s fails, and after it the run is still "running".
	// So the caller is told the cancel did not happen AND the work continues.
	//
	// Retried here so this test covers the SDK's cancel plumbing rather than the
	// platform's spawn timing. Only the 404 is retried: any other error is a
	// real cancel failure and must not be spent down to a green pass.
	var cancelled bool
	cancelDeadline := time.Now().Add(30 * time.Second)
	for {
		cancelled, err = vm.CancelRun(ctx, started.RunID)
		if err == nil {
			break
		}
		if !arker.IsNotFound(err) {
			t.Fatalf("cancel failed for something other than the spawn race: %v", err)
		}
		if time.Now().After(cancelDeadline) {
			t.Fatalf("cancel never took, last error: %v", err)
		}
		time.Sleep(500 * time.Millisecond)
	}
	if !cancelled {
		t.Fatal("cancel reported false")
	}

	// Cancellation is asynchronous; wait for the state rather than a duration.
	deadline := time.Now().Add(2 * time.Minute)
	for {
		record, err := vm.GetRun(ctx, started.RunID)
		if err != nil {
			t.Fatalf("poll cancelled run: %v", err)
		}
		if record.State == "cancelled" || record.State == "failed" || record.State == "completed" {
			t.Logf("cancelled run settled as %q", record.State)
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("run %s still %q two minutes after cancel", started.RunID, record.State)
		}
		time.Sleep(2 * time.Second)
	}
}

func TestRunOutlivingTheSyncWindowIsPolledToCompletion(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	// A one-second sync window guarantees the server backgrounds this run; a
	// synchronous caller must still receive the finished result.
	out, err := vm.Run(ctx, arker.RunRequest{
		Command: "sleep 5; echo slow-done", TimeToBackground: arker.Ptr(1), Timeout: arker.Ptr(120),
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !out.Completed() {
		t.Fatalf("a synchronous call returned a %q result", out.Type)
	}
	if !strings.Contains(out.Stdout, "slow-done") {
		t.Fatalf("stdout was %q", out.Stdout)
	}
}

// ── Sessions ────────────────────────────────────────────────────────────

func TestSessions(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	before, err := vm.ListSessions(ctx, arker.ListSessionsOptions{})
	if err != nil {
		t.Fatalf("list sessions: %v", err)
	}

	created, err := vm.CreateSession(ctx, arker.CreateSessionRequest{
		Env: map[string]string{"SDK_LIVE": "1"}, CWD: "/tmp",
	})
	if err != nil {
		t.Fatalf("create session: %v", err)
	}
	if created.SessionID == "" {
		t.Fatal("create returned no session_id")
	}

	after, err := vm.ListSessions(ctx, arker.ListSessionsOptions{})
	if err != nil {
		t.Fatalf("list after create: %v", err)
	}
	if len(after.Sessions) <= len(before.Sessions) {
		t.Fatalf("create added no session: %d then %d", len(before.Sessions), len(after.Sessions))
	}

	fetched, err := vm.GetSession(ctx, created.SessionID)
	if err != nil {
		t.Fatalf("get session: %v", err)
	}
	if fetched.SessionID != created.SessionID {
		t.Fatalf("get returned %s, want %s", fetched.SessionID, created.SessionID)
	}

	// The session's environment is real, not merely recorded.
	out, err := vm.Run(ctx, arker.RunRequest{Command: "echo $SDK_LIVE; pwd", SessionID: created.SessionID})
	if err != nil {
		t.Fatalf("run in session: %v", err)
	}
	if !strings.Contains(out.Stdout, "1") || !strings.Contains(out.Stdout, "/tmp") {
		t.Fatalf("session env/cwd did not apply: %q", out.Stdout)
	}

	if err := vm.UpdateSession(ctx, created.SessionID, arker.UpdateSessionRequest{
		Cols: arker.Ptr(120), Rows: arker.Ptr(40),
	}); err != nil {
		t.Fatalf("update session: %v", err)
	}

	if err := vm.DeleteSession(ctx, created.SessionID); err != nil {
		t.Fatalf("delete session: %v", err)
	}
	if err := vm.DeleteSession(ctx, created.SessionID); err != nil {
		t.Fatalf("second delete must be a no-op, got: %v", err)
	}
}

// ── Policies ────────────────────────────────────────────────────────────

func TestPolicies(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	if _, err := vm.GetPolicies(ctx); err != nil {
		t.Fatalf("get policies: %v", err)
	}

	doc := arker.PolicyDoc{Policies: []arker.PolicyEntry{
		{Type: "outbound", Match: &arker.PolicyMatch{Hosts: []string{"github.com"}, Ports: []any{443}}, Action: "allow"},
		{Type: "outbound", Action: "deny"},
	}}
	stored, err := vm.SetPolicies(ctx, doc)
	if err != nil {
		t.Fatalf("set policies: %v", err)
	}
	if len(stored.Policies) != len(doc.Policies) {
		t.Fatalf("stored %d rules, sent %d", len(stored.Policies), len(doc.Policies))
	}

	readBack, err := vm.GetPolicies(ctx)
	if err != nil {
		t.Fatalf("re-read policies: %v", err)
	}
	if len(readBack.Policies) != len(doc.Policies) {
		t.Fatalf("policy did not persist: %+v", readBack)
	}

	// An empty document clears back to allow-all.
	cleared, err := vm.SetPolicies(ctx, arker.PolicyDoc{})
	if err != nil {
		t.Fatalf("clear policies: %v", err)
	}
	if len(cleared.Policies) != 0 {
		t.Fatalf("clear left %d rules", len(cleared.Policies))
	}
}

// ── File transfer ───────────────────────────────────────────────────────

func TestFileReadWrite(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	payload := []byte("sdk-live file body\n")
	if err := vm.WriteFile(ctx, "/tmp/sdk-live.txt", payload); err != nil {
		t.Fatalf("write: %v", err)
	}
	got, err := vm.ReadFile(ctx, "/tmp/sdk-live.txt")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(got) != string(payload) {
		t.Fatalf("round trip changed the bytes: %q", got)
	}

	// The guest must see the same file the API reports.
	out, err := vm.Run(ctx, arker.RunRequest{Command: "cat /tmp/sdk-live.txt"})
	if err != nil {
		t.Fatalf("cat: %v", err)
	}
	if out.Stdout != string(payload) {
		t.Fatalf("guest sees %q", out.Stdout)
	}

	// Binary survives the base64 leg intact.
	binary := []byte{0x00, 0x01, 0xff, 0xfe, 0x80}
	if err := vm.WriteFile(ctx, "/tmp/sdk-live.bin", binary); err != nil {
		t.Fatalf("write binary: %v", err)
	}
	back, err := vm.ReadFile(ctx, "/tmp/sdk-live.bin")
	if err != nil {
		t.Fatalf("read binary: %v", err)
	}
	if string(back) != string(binary) {
		t.Fatalf("binary round trip corrupted: %v", back)
	}
}

func TestSyncDir(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	dir := t.TempDir()
	for rel, body := range map[string]string{
		"a.txt": "alpha", "nested/b.txt": "beta", "skip.log": "ignored",
	} {
		path := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	ignore := func(rel string) bool { return strings.HasSuffix(rel, ".log") }

	first, err := vm.SyncDir(ctx, dir, "/tmp/synced", arker.SyncDirOptions{Ignore: ignore})
	if err != nil {
		t.Fatalf("sync_dir: %v", err)
	}
	if first.Sent != 2 {
		t.Fatalf("sent %d files, want 2 (the .log is ignored)", first.Sent)
	}

	out, err := vm.Run(ctx, arker.RunRequest{Command: "cat /tmp/synced/a.txt /tmp/synced/nested/b.txt; ls /tmp/synced"})
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !strings.Contains(out.Stdout, "alpha") || !strings.Contains(out.Stdout, "beta") {
		t.Fatalf("files did not land: %q", out.Stdout)
	}
	if strings.Contains(out.Stdout, "skip.log") {
		t.Fatalf("an ignored file was uploaded: %q", out.Stdout)
	}

	// The manifest is authoritative: an unchanged tree sends nothing.
	second, err := vm.SyncDir(ctx, dir, "/tmp/synced", arker.SyncDirOptions{Ignore: ignore})
	if err != nil {
		t.Fatalf("second sync_dir: %v", err)
	}
	if second.Sent != 0 || second.Skipped != 2 {
		t.Fatalf("an unchanged tree re-sent %d files (skipped %d)", second.Sent, second.Skipped)
	}

	// One changed file ships alone.
	if err := os.WriteFile(filepath.Join(dir, "a.txt"), []byte("alpha-2"), 0o644); err != nil {
		t.Fatal(err)
	}
	third, err := vm.SyncDir(ctx, dir, "/tmp/synced", arker.SyncDirOptions{Ignore: ignore})
	if err != nil {
		t.Fatalf("third sync_dir: %v", err)
	}
	if third.Sent != 1 || third.Skipped != 1 {
		t.Fatalf("delta sync sent %d skipped %d, want 1/1", third.Sent, third.Skipped)
	}
	changed, err := vm.ReadFile(ctx, "/tmp/synced/a.txt")
	if err != nil || string(changed) != "alpha-2" {
		t.Fatalf("changed file is %q (err=%v)", changed, err)
	}
}

// ── Filesystems and syncs ───────────────────────────────────────────────

func TestFilesystemsAndSyncs(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)

	fs, err := h.client.CreateFilesystem(ctx, unique("sdk-live-fs"))
	if err != nil {
		t.Fatalf("create filesystem: %v", err)
	}
	t.Cleanup(func() {
		clean, done := context.WithTimeout(context.Background(), cleanupBudget)
		defer done()
		if err := h.client.DeleteFilesystem(clean, fs.FilesystemID); err != nil {
			t.Errorf("cleanup filesystem %s: %v", fs.FilesystemID, err)
		}
	})

	fetched, err := h.client.GetFilesystem(ctx, fs.FilesystemID)
	if err != nil || fetched.FilesystemID != fs.FilesystemID {
		t.Fatalf("get filesystem: %+v err=%v", fetched, err)
	}

	list, err := h.client.ListFilesystems(ctx, arker.ListFilesystemsOptions{Limit: 100})
	if err != nil {
		t.Fatalf("list filesystems: %v", err)
	}
	if !slices.ContainsFunc(list.Filesystems, func(f arker.Filesystem) bool {
		return f.FilesystemID == fs.FilesystemID
	}) {
		t.Fatalf("filesystem %s missing from a %d-row listing", fs.FilesystemID, len(list.Filesystems))
	}

	vm := h.fork(t, ctx)
	sync, err := vm.CreateSync(ctx, fs.FilesystemID, "/mnt/shared")
	if err != nil {
		t.Fatalf("create sync: %v", err)
	}
	syncs, err := vm.ListSyncs(ctx, arker.ListSyncsOptions{})
	if err != nil {
		t.Fatalf("list syncs: %v", err)
	}
	if !slices.ContainsFunc(syncs.Syncs, func(s arker.Sync) bool { return s.SyncID == sync.SyncID }) {
		t.Fatalf("sync %s missing from the VM's listing", sync.SyncID)
	}
	if err := vm.DeleteSync(ctx, sync.SyncID); err != nil {
		t.Fatalf("delete sync: %v", err)
	}
}

// ── Interactive PTY ─────────────────────────────────────────────────────

func TestPTY(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)
	vm := h.fork(t, ctx)

	var mu sync.Mutex
	var seen strings.Builder
	pty, err := vm.ConnectPTY(ctx, arker.PTYOptions{
		Cols: arker.Ptr(100), Rows: arker.Ptr(30),
		OnData: func(b []byte) {
			mu.Lock()
			defer mu.Unlock()
			seen.Write(b)
		},
	})
	if err != nil {
		t.Fatalf("connect pty: %v", err)
	}
	defer func() { _ = pty.Close() }()

	marker := unique("pty-live")
	if err := pty.SendString("echo " + marker + "\n"); err != nil {
		t.Fatalf("send: %v", err)
	}

	deadline := time.Now().Add(90 * time.Second)
	for {
		mu.Lock()
		got := seen.String()
		mu.Unlock()
		if strings.Contains(got, marker) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("marker never echoed; saw %q", got)
		}
		time.Sleep(500 * time.Millisecond)
	}

	if err := pty.Resize(80, 24); err != nil {
		t.Fatalf("resize: %v", err)
	}
	if err := pty.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	select {
	case <-pty.Done():
	case <-time.After(30 * time.Second):
		t.Fatal("Done never closed after Close")
	}
}
