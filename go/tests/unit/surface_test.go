package unit

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ArkerHQ/arker-sdk/go"
)

var fastRetry = &arker.Retry{Attempts: 4, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond}

// The real GET /v1/runs envelope. Writing a stub the API cannot produce is how
// a decode bug reaches production behind a green test.
const orgRunsBody = `{"since":1,"until":2,"limit":50,"offset":0,"lite":false,"rows":[` +
	`{"t_ms":1700000000000,"request_id":"req_1","run_id":"run_1","vm_id":"vm_1","session_id":"s1",` +
	`"region":"us-west-2","provider":"aws","status":200,"total_ms":12.5,"queue_ms":1.5,` +
	`"executor_duration_ms":9,"executor_kind":"fc","executor_cpu_ms":8,"executor_mem_mb":64,` +
	`"vm_vcpus":2,"vm_memory_mib":4096,"path":"/v1/vms/vm_1/runs","method":"POST","command":"echo hi",` +
	`"source_vm_id":"vm_0","exit_code":0,"endpoint":"runs","api_key_prefix":"ark_live_ab",` +
	`"body_bytes_in":10,"body_bytes_out":20,"body_in":"","body_out":""}]}`

// twoPlane starts a regional and a control-plane server so a test can assert
// which one a call reached. Mixing them up is invisible against one server and
// fatal in production: the control host does not route the regional paths.
func twoPlane(t *testing.T, regional, control http.HandlerFunc) *arker.Client {
	t.Helper()
	rs := httptest.NewServer(regional)
	cs := httptest.NewServer(control)
	t.Cleanup(rs.Close)
	t.Cleanup(cs.Close)
	c, err := arker.New(arker.Options{
		APIKey: "k", BaseURL: rs.URL, ControlBaseURL: cs.URL, Retry: fastRetry,
	})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	return c
}

func reject(t *testing.T, plane string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("%s %s unexpectedly reached the %s plane", r.Method, r.URL.Path, plane)
		w.WriteHeader(http.StatusTeapot)
	}
}

// ── Routing ─────────────────────────────────────────────────────────────

func TestOrgWideCallsGoToTheControlPlane(t *testing.T) {
	for _, tc := range []struct {
		name, path string
		body       string
		call       func(*arker.Client) error
	}{
		{"ListVMs", "/v1/vms", `{"vms":[]}`, func(c *arker.Client) error {
			_, err := c.ListVMs(context.Background(), arker.ListVMsOptions{})
			return err
		}},
		{"ListRuns", "/v1/runs", orgRunsBody, func(c *arker.Client) error {
			_, err := c.ListRuns(context.Background(), arker.ListOrgRunsOptions{})
			return err
		}},
		{"ListRegions", "/v1/regions", `{"regions":[]}`, func(c *arker.Client) error {
			_, err := c.ListRegions(context.Background())
			return err
		}},
		{"Whoami", "/v1/whoami", `{"org_id":"o","org_name":"n"}`, func(c *arker.Client) error {
			_, err := c.Whoami(context.Background())
			return err
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var got string
			c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
				got = r.URL.Path
				fmt.Fprint(w, tc.body)
			})
			if err := tc.call(c); err != nil {
				t.Fatalf("%s: %v", tc.name, err)
			}
			if got != tc.path {
				t.Fatalf("control plane saw %q, want %q", got, tc.path)
			}
		})
	}
}

func TestFilesystemsStayRegional(t *testing.T) {
	// The control-plane host does not route /v1/filesystems; the regional
	// endpoint does. Sending them to the control plane 404s in production and
	// looks exactly like an empty org.
	var got string
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		got = r.URL.Path
		fmt.Fprint(w, `{"filesystems":[]}`)
	}, reject(t, "control"))

	if _, err := c.ListFilesystems(context.Background(), arker.ListFilesystemsOptions{}); err != nil {
		t.Fatalf("list: %v", err)
	}
	if got != "/v1/filesystems" {
		t.Fatalf("regional plane saw %q", got)
	}
}

func TestControlCallsWorkWithNoPlacement(t *testing.T) {
	// A client with no provider/region can still answer org-wide questions --
	// that is what makes Whoami usable before you know where to fork.
	cs := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"org_id":"org_1","org_name":"acme"}`)
	}))
	defer cs.Close()
	c, err := arker.New(arker.Options{APIKey: "k", ControlBaseURL: cs.URL, Retry: fastRetry})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	who, err := c.Whoami(context.Background())
	if err != nil {
		t.Fatalf("whoami: %v", err)
	}
	if who.OrgID != "org_1" {
		t.Fatalf("org_id %q", who.OrgID)
	}
	// The regional half must fail loudly rather than guess an endpoint.
	if _, err := c.Fork(context.Background(), arker.ForkRequest{SourceVMName: "base"}); err == nil {
		t.Fatal("fork without placement reported success")
	}
}

func TestProviderAndRegionAreRequiredTogether(t *testing.T) {
	if _, err := arker.New(arker.Options{APIKey: "k", Provider: "aws"}); err == nil {
		t.Fatal("a provider with no region was accepted; it would silently pick a wrong endpoint")
	}
}

func TestPlacementDerivesTheRegionalURL(t *testing.T) {
	c, err := arker.New(arker.Options{APIKey: "k", Provider: "aws", Region: "us-east-1"})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	base, err := c.BaseURL()
	if err != nil {
		t.Fatalf("base url: %v", err)
	}
	if base != "https://aws-us-east-1.arker.ai/api" {
		t.Fatalf("derived %q", base)
	}
}

func TestListedVMsCarryTheirOwnEndpoint(t *testing.T) {
	// ListVMs aggregates across regions, so a returned handle must address the
	// VM's OWN region. Reusing the client's endpoint would send every call for
	// a remote VM to the wrong worker.
	c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"vms":[{"vm_id":"vm_far","provider":"gcp","region":"us-central1"}]}`)
	})
	list, err := c.ListVMs(context.Background(), arker.ListVMsOptions{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list.VMs) != 1 {
		t.Fatalf("got %d vms", len(list.VMs))
	}
	if got := list.VMs[0].BaseURL(); got != "https://gcp-us-central1.arker.ai/api" {
		t.Fatalf("remote VM bound to %q", got)
	}
}

// ── VM lifecycle ────────────────────────────────────────────────────────

func TestUpdateClearsDescriptionWithAnExplicitNull(t *testing.T) {
	// Omitting the field means "leave it alone"; only an explicit null clears
	// it. Without the distinction there is no way to remove a description.
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, forkVM)
	}, reject(t, "control"))

	if _, err := c.VM("vm_1").Update(context.Background(), arker.UpdateRequest{ClearDescription: true}); err != nil {
		t.Fatalf("update: %v", err)
	}
	value, present := body["description"]
	if !present {
		t.Fatal("ClearDescription sent no description key, so the field is left unchanged")
	}
	if value != nil {
		t.Fatalf("description was %v, want an explicit null", value)
	}
}

func TestUpdateLeavesDescriptionAloneByDefault(t *testing.T) {
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, forkVM)
	}, reject(t, "control"))

	memory := 2048
	_, err := c.VM("vm_1").Update(context.Background(), arker.UpdateRequest{
		Resources: &arker.Resources{MemoryMiB: &memory},
	})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if _, present := body["description"]; present {
		t.Fatal("a resource-only update sent description, which would clear it")
	}
}

// ── Runs ────────────────────────────────────────────────────────────────

func TestRunPollsABackgroundedRunToCompletion(t *testing.T) {
	// The server backgrounds a run that outlives its sync window. A
	// synchronous caller must still receive the finished run, not the ack.
	var polls int32
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			// 202 Accepted is how the API says "backgrounded"; a completed run
			// comes back 200.
			w.WriteHeader(http.StatusAccepted)
			fmt.Fprint(w, `{"run_id":"run_1","state":"running","session_id":"s1"}`)
			return
		}
		if atomic.AddInt32(&polls, 1) < 2 {
			fmt.Fprint(w, `{"run_id":"run_1","state":"running","stdout":"partial","stdout_encoding":"utf-8"}`)
			return
		}
		fmt.Fprint(w, `{"run_id":"run_1","state":"completed","exit_code":0,"stdout":"done\n","stdout_encoding":"utf-8"}`)
	}, reject(t, "control"))

	out, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Command: "sleep 1"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !out.Completed() {
		t.Fatalf("run returned type %q; a synchronous caller must get the finished run", out.Type)
	}
	if out.Stdout != "done\n" || out.ExitCode == nil || *out.ExitCode != 0 {
		t.Fatalf("stdout=%q exit=%v", out.Stdout, out.ExitCode)
	}
	if atomic.LoadInt32(&polls) < 2 {
		t.Fatalf("polled %d times; it stopped before the run was terminal", polls)
	}
}

func TestExplicitBackgroundReturnsTheAckWithoutPolling(t *testing.T) {
	var polls int32
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			atomic.AddInt32(&polls, 1)
		}
		w.WriteHeader(http.StatusAccepted)
		fmt.Fprint(w, `{"run_id":"run_1","state":"running","session_id":"s1"}`)
	}, reject(t, "control"))

	out, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{
		Command: "sleep 600", TimeToBackground: arker.Ptr(0),
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if out.Completed() {
		t.Fatal("TimeToBackground=0 must hand back the running ack, not wait")
	}
	if out.RunID != "run_1" || out.SessionID != "s1" {
		t.Fatalf("ack lost its identifiers: %+v", out)
	}
	if polls != 0 {
		t.Fatalf("polled %d times despite an explicit background request", polls)
	}
}

func TestBase64OutputIsDecoded(t *testing.T) {
	// The service base64-encodes output that is not valid UTF-8. Handing the
	// caller the encoded string would silently corrupt every binary result.
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"run_id":"r","state":"completed","exit_code":0,"stdout":"AAECgA==","stdout_encoding":"base64"}`)
	}, reject(t, "control"))

	out, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Command: "cat blob"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	want := []byte{0x00, 0x01, 0x02, 0x80}
	if string(out.StdoutBytes) != string(want) {
		t.Fatalf("StdoutBytes = %v, want %v", out.StdoutBytes, want)
	}
}

func TestRunSendsAcquireAndReleaseAsCSV(t *testing.T) {
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, `{"run_id":"r","state":"completed","exit_code":0}`)
	}, reject(t, "control"))

	_, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{
		Command: "true", Acquire: []string{"gpu", "net"},
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if body["acquire"] != "gpu,net" {
		t.Fatalf("acquire was %v, want the comma-joined form the API takes", body["acquire"])
	}
}

func TestCancelRunReportsTheServersAnswer(t *testing.T) {
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || !strings.HasSuffix(r.URL.Path, "/runs/run_9") {
			t.Errorf("cancel hit %s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"cancelled":true}`)
	}, reject(t, "control"))

	cancelled, err := c.VM("vm_1").CancelRun(context.Background(), "run_9")
	if err != nil || !cancelled {
		t.Fatalf("cancelled=%v err=%v", cancelled, err)
	}
}

// ── Sessions and policies ───────────────────────────────────────────────

func TestSessionRoutesAreScopedToTheVM(t *testing.T) {
	seen := map[string]string{}
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		seen[r.Method] = r.URL.Path
		fmt.Fprint(w, `{"session_id":"s1","ok":true,"deleted":true}`)
	}, reject(t, "control"))

	vm := c.VM("vm_1")
	ctx := context.Background()
	if _, err := vm.GetSession(ctx, "s1"); err != nil {
		t.Fatalf("get: %v", err)
	}
	if err := vm.UpdateSession(ctx, "s1", arker.UpdateSessionRequest{Cols: arker.Ptr(120)}); err != nil {
		t.Fatalf("update: %v", err)
	}
	if err := vm.DeleteSession(ctx, "s1"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	for method, want := range map[string]string{
		http.MethodGet: "/v1/vms/vm_1/sessions/s1", http.MethodPatch: "/v1/vms/vm_1/sessions/s1",
		http.MethodDelete: "/v1/vms/vm_1/sessions/s1",
	} {
		if seen[method] != want {
			t.Fatalf("%s went to %q, want %q", method, seen[method], want)
		}
	}
}

func TestSetPoliciesReplacesTheDocument(t *testing.T) {
	var method, path string
	var sent arker.PolicyDoc
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		method, path = r.Method, r.URL.Path
		_ = json.NewDecoder(r.Body).Decode(&sent)
		fmt.Fprint(w, `{"policies":[{"type":"outbound","action":"deny"}],"hostname":"vm.example"}`)
	}, reject(t, "control"))

	doc := arker.PolicyDoc{Policies: []arker.PolicyEntry{
		{Type: "outbound", Match: &arker.PolicyMatch{Hosts: []string{"github.com"}}, Action: "allow"},
		{Type: "outbound", Action: "deny"},
	}}
	out, err := c.VM("vm_1").SetPolicies(context.Background(), doc)
	if err != nil {
		t.Fatalf("set: %v", err)
	}
	if method != http.MethodPut || path != "/v1/vms/vm_1/policies" {
		t.Fatalf("%s %s", method, path)
	}
	if len(sent.Policies) != 2 || sent.Policies[0].Match.Hosts[0] != "github.com" {
		t.Fatalf("policy body did not round-trip: %+v", sent)
	}
	if out.Hostname != "vm.example" {
		t.Fatalf("response-only hostname dropped: %+v", out)
	}
}

// ── Filesystem transfer ─────────────────────────────────────────────────

func TestReadFileDecodesInlineBase64(t *testing.T) {
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"ok":true,"op":"read","path":"/f","size":3,"content":"aGVsbG8=","encoding":"base64"}`)
	}, reject(t, "control"))

	data, err := c.VM("vm_1").ReadFile(context.Background(), "/f")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(data) != "hello" {
		t.Fatalf("got %q", data)
	}
}

func TestReadFileFollowsAPresignedURL(t *testing.T) {
	blob := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "large-file-body")
	}))
	defer blob.Close()

	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"ok":true,"op":"read","path":"/big","size":15,"presigned_url":%q,"method":"GET","expires_in":300}`, blob.URL)
	}, reject(t, "control"))

	data, err := c.VM("vm_1").ReadFile(context.Background(), "/big")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(data) != "large-file-body" {
		t.Fatalf("got %q", data)
	}
}

func TestWriteFileStreamsWithAnExactSize(t *testing.T) {
	// The router reads ?size= to decide whether to forward the body streamed,
	// so a wrong value breaks the transfer rather than merely mis-reporting it.
	var size, path string
	var got []byte
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		size, path = r.URL.Query().Get("size"), r.URL.Query().Get("path")
		got, _ = io.ReadAll(r.Body)
		fmt.Fprint(w, `{"ok":true}`)
	}, reject(t, "control"))

	payload := []byte("some bytes")
	if err := c.VM("vm_1").WriteFile(context.Background(), "/tmp/x", payload); err != nil {
		t.Fatalf("write: %v", err)
	}
	if path != "/tmp/x" || size != "10" {
		t.Fatalf("path=%q size=%q", path, size)
	}
	if string(got) != string(payload) {
		t.Fatalf("body was %q", got)
	}
}

func TestSyncDirSendsOnlyWhatTheManifestLacks(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "same.txt", "unchanged")
	write(t, dir, "nested/new.txt", "brand new")

	// The manifest is authoritative: same.txt matches, so only new.txt ships.
	sameHash := sha256Hex("unchanged")
	var extracted []string
	var extractMode string
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/sync") {
			fmt.Fprintf(w, `{"root":"/dst","hash_algo":"sha256","truncated":false,"entries":[{"path":"same.txt","size":9,"mode":420,"hash":%q}]}`, sameHash)
			return
		}
		extractMode = r.URL.Query().Get("extract")
		extracted = tarNames(t, r.Body, extractMode)
		fmt.Fprint(w, `{"ok":true}`)
	}, reject(t, "control"))

	result, err := c.VM("vm_1").SyncDir(context.Background(), dir, "dst", arker.SyncDirOptions{})
	if err != nil {
		t.Fatalf("sync_dir: %v", err)
	}
	if result.Sent != 1 || result.Skipped != 1 {
		t.Fatalf("sent=%d skipped=%d; the manifest match should have been skipped", result.Sent, result.Skipped)
	}
	if len(extracted) != 1 || extracted[0] != "nested/new.txt" {
		t.Fatalf("tarball carried %v, want only nested/new.txt", extracted)
	}
	if extractMode == "" {
		t.Fatal("extract mode was not sent, so the guest would not untar")
	}
}

func TestSyncDirRespectsIgnore(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "keep.txt", "a")
	write(t, dir, "skip.log", "b")

	var names []string
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/sync") {
			fmt.Fprint(w, `{"root":"/dst","hash_algo":"sha256","truncated":false,"entries":[]}`)
			return
		}
		names = tarNames(t, r.Body, r.URL.Query().Get("extract"))
		fmt.Fprint(w, `{"ok":true}`)
	}, reject(t, "control"))

	result, err := c.VM("vm_1").SyncDir(context.Background(), dir, "dst", arker.SyncDirOptions{
		Ignore: func(rel string) bool { return strings.HasSuffix(rel, ".log") },
	})
	if err != nil {
		t.Fatalf("sync_dir: %v", err)
	}
	// Ignored before hashing: it is neither sent nor counted as skipped.
	if result.Sent != 1 || result.Skipped != 0 {
		t.Fatalf("sent=%d skipped=%d", result.Sent, result.Skipped)
	}
	if len(names) != 1 || names[0] != "keep.txt" {
		t.Fatalf("tarball carried %v", names)
	}
}

func TestSyncDirReportsATruncatedManifest(t *testing.T) {
	// Past the server's walk cap every omitted file looks absent, so the delta
	// sync silently becomes a full one. The caller has to be able to see that.
	dir := t.TempDir()
	write(t, dir, "a.txt", "a")
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/sync") {
			fmt.Fprint(w, `{"root":"/dst","hash_algo":"sha256","truncated":true,"entries":[]}`)
			return
		}
		fmt.Fprint(w, `{"ok":true}`)
	}, reject(t, "control"))

	result, err := c.VM("vm_1").SyncDir(context.Background(), dir, "dst", arker.SyncDirOptions{})
	if err != nil {
		t.Fatalf("sync_dir: %v", err)
	}
	if !result.ManifestTruncated {
		t.Fatal("a truncated manifest was not surfaced")
	}
}

func TestSyncDirCacheSkipsRehashingButNotUploads(t *testing.T) {
	// The cache is a pure accelerator. It must never decide remote state: with
	// an empty manifest the file is uploaded again even though it is cached.
	dir := t.TempDir()
	write(t, dir, "a.txt", "a")
	cache := arker.NewSyncCache()

	var uploads int32
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/sync") {
			fmt.Fprint(w, `{"root":"/dst","hash_algo":"sha256","truncated":false,"entries":[]}`)
			return
		}
		atomic.AddInt32(&uploads, 1)
		fmt.Fprint(w, `{"ok":true}`)
	}, reject(t, "control"))

	for range 2 {
		if _, err := c.VM("vm_1").SyncDir(context.Background(), dir, "dst",
			arker.SyncDirOptions{Cache: cache}); err != nil {
			t.Fatalf("sync_dir: %v", err)
		}
	}
	if uploads != 2 {
		t.Fatalf("uploaded %d times; the cache must not suppress a send the manifest asked for", uploads)
	}
}

// ── Helpers ─────────────────────────────────────────────────────────────

func write(t *testing.T, dir, rel, body string) {
	t.Helper()
	path := filepath.Join(dir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func tarNames(t *testing.T, body io.Reader, mode string) []string {
	t.Helper()
	if mode == "tar.gz" {
		gz, err := gzip.NewReader(body)
		if err != nil {
			t.Fatalf("gzip: %v", err)
		}
		body = gz
	}
	var names []string
	tr := tar.NewReader(body)
	for {
		header, err := tr.Next()
		if err == io.EOF {
			return names
		}
		if err != nil {
			t.Fatalf("tar: %v", err)
		}
		names = append(names, header.Name)
	}
}

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// ── Regressions ─────────────────────────────────────────────────────────

func TestListRunsDecodesTheRowsEnvelope(t *testing.T) {
	// GET /v1/runs answers {since,until,limit,offset,lite,rows} -- there is no
	// "runs" key and no cursor. Decoding the wrong field is silent: the call
	// succeeds and returns an empty page forever.
	c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, orgRunsBody)
	})
	list, err := c.ListRuns(context.Background(), arker.ListOrgRunsOptions{})
	if err != nil {
		t.Fatalf("list runs: %v", err)
	}
	if len(list.Rows) != 1 {
		t.Fatalf("decoded %d rows from a 1-row page", len(list.Rows))
	}
	row := list.Rows[0]
	if row.RunID != "run_1" || row.RequestID != "req_1" || row.Status != 200 {
		t.Fatalf("row did not decode: %+v", row)
	}
	if row.ExecutorKind != "fc" || row.TotalMs != 12.5 {
		t.Fatalf("telemetry fields lost: %+v", row)
	}
	// The server fills in the window it actually used.
	if list.Limit != 50 || list.Since != 1 {
		t.Fatalf("echoed window lost: %+v", list)
	}
}

func TestBackgroundAckWithoutStateIsNotReportedAsCompleted(t *testing.T) {
	// `state` is OPTIONAL on BackgroundRunResponse -- only run_id is required.
	// Sniffing the body cannot tell that ack apart from a completed run with no
	// output, so the 202 is the discriminator. Getting this wrong hands the
	// caller a "successful run" with empty output and no exit code.
	var polls int32
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusAccepted)
			fmt.Fprint(w, `{"run_id":"run_1","session_id":"s1"}`)
			return
		}
		atomic.AddInt32(&polls, 1)
		fmt.Fprint(w, `{"run_id":"run_1","state":"completed","exit_code":0,"stdout":"done\n","stdout_encoding":"utf-8"}`)
	}, reject(t, "control"))

	out, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Command: "sleep 1"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !out.Completed() || out.Stdout != "done\n" {
		t.Fatalf("a stateless 202 ack was not polled to completion: %+v", out)
	}
	if polls == 0 {
		t.Fatal("never polled; the ack was mistaken for a finished run")
	}
}

func TestExplicitBackgroundHandlesAStatelessAck(t *testing.T) {
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
		fmt.Fprint(w, `{"run_id":"run_1","session_id":"s1"}`)
	}, reject(t, "control"))

	out, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{
		Command: "sleep 600", TimeToBackground: arker.Ptr(0),
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if out.Completed() {
		t.Fatalf("a 202 ack was typed %q", out.Type)
	}
	if out.State != "running" {
		t.Fatalf("state %q; an ack with no state is running", out.State)
	}
}

func TestNegativeExitCodeIsAFailure(t *testing.T) {
	// A negative exit code means no process status was obtained -- killed, or
	// the compute was lost. Python and TypeScript both call that failed.
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"run_id":"r","state":"completed","exit_code":-9,"stdout":"","stdout_encoding":"utf-8"}`)
	}, reject(t, "control"))

	out, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Command: "sleep 999"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if out.State != "failed" {
		t.Fatalf("exit -9 reported as %q", out.State)
	}
}

func TestEmptySSHKeyListClearsRatherThanOmits(t *testing.T) {
	// The API reads an empty array as "remove all keys". A plain slice with
	// omitempty drops exactly that value, making the documented behavior
	// unreachable.
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, forkVM)
	}, reject(t, "control"))

	if _, err := c.VM("vm_1").Update(context.Background(), arker.UpdateRequest{
		SSHPublicKeys: &[]string{},
	}); err != nil {
		t.Fatalf("update: %v", err)
	}
	keys, present := body["ssh_public_keys"]
	if !present {
		t.Fatal("an empty key list was omitted, so the keys are left unchanged")
	}
	if list, ok := keys.([]any); !ok || len(list) != 0 {
		t.Fatalf("ssh_public_keys was %v, want an empty array", keys)
	}
}

func TestNilSSHKeyListLeavesKeysAlone(t *testing.T) {
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, forkVM)
	}, reject(t, "control"))

	if _, err := c.VM("vm_1").Update(context.Background(), arker.UpdateRequest{
		Description: arker.Ptr("just a rename"),
	}); err != nil {
		t.Fatalf("update: %v", err)
	}
	if _, present := body["ssh_public_keys"]; present {
		t.Fatal("a rename sent ssh_public_keys, which would rewrite the keys")
	}
}

func TestSyncReadIsRetriedOnATransportFailure(t *testing.T) {
	// The sync read and manifest ops are READS carried over POST. There is no
	// outcome to be unknown about, so a dropped connection must be retried
	// rather than surfaced as UnknownOutcomeError.
	var n int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&n, 1) == 1 {
			hj, _ := w.(http.Hijacker)
			conn, _, _ := hj.Hijack()
			_ = conn.Close()
			return
		}
		fmt.Fprint(w, `{"ok":true,"op":"read","path":"/f","size":5,"content":"hello","encoding":"utf-8"}`)
	}))
	defer srv.Close()
	c, _ := arker.New(arker.Options{APIKey: "k", BaseURL: srv.URL, Retry: fastRetry})

	data, err := c.VM("vm_1").ReadFile(context.Background(), "/f")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(data) != "hello" {
		t.Fatalf("got %q", data)
	}
	if atomic.LoadInt32(&n) != 2 {
		t.Fatalf("a read gave up after %d attempt(s)", n)
	}
}

func TestDiscoverRegionsSendsNoAuthorization(t *testing.T) {
	// It reads the PUBLIC catalog. A "Bearer " with no token is worse than an
	// absent header: a gateway can reject it where anonymous would pass.
	var header string
	var present bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		header, present = r.Header.Get("Authorization"), r.Header.Values("Authorization") != nil
		fmt.Fprint(w, `{"regions":[]}`)
	}))
	defer srv.Close()

	if _, err := arker.DiscoverRegions(context.Background(), srv.URL); err != nil {
		t.Fatalf("discover: %v", err)
	}
	if present {
		t.Fatalf("sent Authorization: %q", header)
	}
}

func TestExhaustedTransportRetriesDoNotSleepPastTheLastAttempt(t *testing.T) {
	// The final failure used to sleep a full backoff before giving up, adding
	// up to MaxDelay of dead latency to every read that exhausts its budget.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hj, _ := w.(http.Hijacker)
		conn, _, _ := hj.Hijack()
		_ = conn.Close()
	}))
	defer srv.Close()
	c, _ := arker.New(arker.Options{APIKey: "k", BaseURL: srv.URL,
		Retry: &arker.Retry{Attempts: 3, BaseDelay: 300 * time.Millisecond, MaxDelay: time.Second}})

	start := time.Now()
	_, _, _ = c.GetVM(context.Background(), "vm_1")
	// Two sleeps (300ms + 600ms) between three attempts, not three.
	if elapsed := time.Since(start); elapsed > 1500*time.Millisecond {
		t.Fatalf("exhausted retries took %v; it slept after the final attempt", elapsed)
	}
}
