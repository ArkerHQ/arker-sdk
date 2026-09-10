package arker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

const forkVM = `{"vm_id":"vm_child","owner_org_id":"org","state":"idle"}`

func testClient(t *testing.T, h http.HandlerFunc) (*Client, func()) {
	t.Helper()
	srv := httptest.NewServer(h)
	c, err := New(Options{
		APIKey:  "ark_live_test",
		BaseURL: srv.URL,
		Retry:   &Retry{Attempts: 4, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond},
	})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	return c, srv.Close
}

// ── Idempotency ─────────────────────────────────────────────────────────

func TestForkSendsAnIdempotencyKeyWithoutBeingAsked(t *testing.T) {
	var key string
	var body map[string]any
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		key = r.Header.Get("Idempotency-Key")
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, forkVM)
	})
	defer done()

	if _, err := c.Fork(context.Background(), ForkRequest{SourceVMName: "base"}); err != nil {
		t.Fatalf("fork: %v", err)
	}
	if key == "" {
		t.Fatal("fork sent no Idempotency-Key")
	}
	if len(key) > 64 {
		t.Fatalf("generated key is %d chars; the server rejects >64", len(key))
	}
	// A header, not a contract field: the server's validator rejects unknown
	// body keys, so a leak here would 400 every fork.
	if _, leaked := body["IdempotencyKey"]; leaked {
		t.Fatal("IdempotencyKey leaked into the request body")
	}
	if _, leaked := body["idempotency_key"]; leaked {
		t.Fatal("idempotency_key leaked into the request body")
	}
}

func TestForkRetryReusesTheSameIdempotencyKey(t *testing.T) {
	// The load-bearing one. A fresh key per attempt looks identical in every
	// other test -- a key is sent, it is well formed, the fork succeeds -- and
	// still builds the duplicate VM this exists to prevent.
	var keys []string
	var n int32
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		keys = append(keys, r.Header.Get("Idempotency-Key"))
		if atomic.AddInt32(&n, 1) == 1 {
			w.WriteHeader(http.StatusBadGateway)
			fmt.Fprint(w, `{"error":{"code":"bad_gateway","message":"lost"}}`)
			return
		}
		fmt.Fprint(w, forkVM)
	})
	defer done()

	if _, err := c.Fork(context.Background(), ForkRequest{SourceVMName: "base"}); err != nil {
		t.Fatalf("fork: %v", err)
	}
	if len(keys) != 2 {
		t.Fatalf("expected a retry, got %d attempt(s)", len(keys))
	}
	// Non-empty as well as equal: two blank keys are also "the same key".
	if keys[0] == "" {
		t.Fatal("fork sent an empty Idempotency-Key")
	}
	if keys[0] != keys[1] {
		t.Fatalf("retry changed the key: %q then %q", keys[0], keys[1])
	}
}

func TestForkUsesAnExplicitKeyVerbatim(t *testing.T) {
	var key string
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		key = r.Header.Get("Idempotency-Key")
		fmt.Fprint(w, forkVM)
	})
	defer done()

	_, err := c.Fork(context.Background(), ForkRequest{SourceVMName: "base", IdempotencyKey: "caller-chosen"})
	if err != nil {
		t.Fatalf("fork: %v", err)
	}
	if key != "caller-chosen" {
		t.Fatalf("key was %q, want caller-chosen", key)
	}
}

func TestTwoForksDoNotShareAGeneratedKey(t *testing.T) {
	var keys []string
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		keys = append(keys, r.Header.Get("Idempotency-Key"))
		fmt.Fprint(w, forkVM)
	})
	defer done()

	for range 2 {
		if _, err := c.Fork(context.Background(), ForkRequest{SourceVMName: "base"}); err != nil {
			t.Fatalf("fork: %v", err)
		}
	}
	if keys[0] == keys[1] {
		t.Fatal("separate forks shared a key; two deliberate machines would collapse into one")
	}
}

// ── Retry safety ────────────────────────────────────────────────────────

func TestMutationIsNotRetriedOnTransportFailure(t *testing.T) {
	// The 2026-07-31 shape: the server acts, the response is lost. Retrying
	// blind is how one fork becomes two VMs. The caller must be told the
	// outcome is unknown instead.
	var n int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&n, 1)
		// Kill the connection after the server has "acted".
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Fatal("no hijacker")
		}
		conn, _, _ := hj.Hijack()
		_ = conn.Close()
	}))
	defer srv.Close()

	c, _ := New(Options{APIKey: "k", BaseURL: srv.URL,
		Retry: &Retry{Attempts: 4, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond}})

	_, err := c.Fork(context.Background(), ForkRequest{SourceVMName: "base"})
	if err == nil {
		t.Fatal("a lost response reported success")
	}
	var unknown *UnknownOutcomeError
	if !errors.As(err, &unknown) {
		t.Fatalf("want UnknownOutcomeError, got %T: %v", err, err)
	}
	if got := atomic.LoadInt32(&n); got != 1 {
		t.Fatalf("mutation was retried %d times; the outcome was unknown", got)
	}
}

func TestReadIsRetriedOnTransportFailure(t *testing.T) {
	// A GET is safe to repeat -- no outcome to be unknown about.
	var n int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&n, 1) == 1 {
			hj, _ := w.(http.Hijacker)
			conn, _, _ := hj.Hijack()
			_ = conn.Close()
			return
		}
		fmt.Fprint(w, forkVM)
	}))
	defer srv.Close()

	c, _ := New(Options{APIKey: "k", BaseURL: srv.URL,
		Retry: &Retry{Attempts: 4, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond}})

	if _, _, err := c.GetVM(context.Background(), "vm_1"); err != nil {
		t.Fatalf("get: %v", err)
	}
	if got := atomic.LoadInt32(&n); got != 2 {
		t.Fatalf("expected a retry on a read, got %d attempt(s)", got)
	}
}

func TestServerRetryableFalseBeatsTheStatus(t *testing.T) {
	// arkerd's `retryable` is the authority; the status is only a fallback,
	// because the field is frequently absent.
	var n int32
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&n, 1)
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, `{"error":{"code":"unavailable","message":"no","retryable":false}}`)
	})
	defer done()

	if _, _, err := c.GetVM(context.Background(), "vm_1"); err == nil {
		t.Fatal("expected an error")
	}
	if got := atomic.LoadInt32(&n); got != 1 {
		t.Fatalf("retryable:false was retried %d times", got)
	}
}

// ── Error shape ─────────────────────────────────────────────────────────

func TestDeleteTreatsAbsentAsSuccess(t *testing.T) {
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"error":{"code":"not_found","message":"gone"}}`)
	})
	defer done()

	if err := c.DeleteVM(context.Background(), "vm_gone"); err != nil {
		t.Fatalf("delete of an absent VM must be success, got: %v", err)
	}
}

func TestConflictIsTyped(t *testing.T) {
	c, done := testClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"error":{"code":"conflict","message":"key reused"}}`)
	})
	defer done()

	_, err := c.Fork(context.Background(), ForkRequest{SourceVMName: "base"})
	if !IsConflict(err) {
		t.Fatalf("want a typed conflict, got %T: %v", err, err)
	}
}
