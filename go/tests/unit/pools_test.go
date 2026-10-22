package unit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

func TestPoolsV2(t *testing.T) {
	var paths []string
	var bodies []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		var body map[string]any
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&body)
		}
		bodies = append(bodies, body)
		switch r.URL.Path {
		case "/v1/pools/p/workers":
			fmt.Fprint(w, `{"workers":[]}`)
		default:
			fmt.Fprint(w, `{"pool_id":"p","type":"managed","provider":"aws","platform":"icelake","state":"running","reason":null,"resources_reserved":{"vcpu":8,"memory_mib":0,"disk_mib":0},"resources_provisioned":{"vcpu":0,"memory_mib":0,"disk_mib":0},"commitments":[{"starts_at":"2026-01-01T00:00:00Z","ends_at":"2026-01-02T00:00:00Z"}]}`)
		}
	}))
	defer srv.Close()
	c, err := arker.New(arker.Options{APIKey: "k", BaseURL: srv.URL, Retry: fastRetry})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	req := arker.CreatePoolRequest{Type: "managed", Platform: "icelake", Resources: &arker.PoolResources{VCPU: 8}}
	pool, err := c.CreatePool(ctx, req)
	if err != nil || pool.PoolID != "p" {
		t.Fatalf("%+v %v", pool, err)
	}
	commitment := arker.PoolCommitment{StartsAt: "2027-01-01T00:00:00Z", EndsAt: "2027-01-02T00:00:00Z"}
	if _, err = c.AddPoolCommitment(ctx, "p", commitment); err != nil {
		t.Fatal(err)
	}
	if _, err = c.RenamePool(ctx, "p", nil); err != nil {
		t.Fatal(err)
	}
	if _, err = c.ListPoolWorkers(ctx, "p"); err != nil {
		t.Fatal(err)
	}
	if _, err = c.DeletePool(ctx, "p"); err != nil {
		t.Fatal(err)
	}
	want := []string{"POST /v1/pools", "GET /v1/pools/p", "PATCH /v1/pools/p", "PATCH /v1/pools/p", "GET /v1/pools/p/workers", "DELETE /v1/pools/p"}
	if !reflect.DeepEqual(paths, want) {
		t.Fatal(paths)
	}
	if bodies[0]["resources"].(map[string]any)["disk_mib"] != float64(0) {
		t.Fatal(bodies[0])
	}
	if len(bodies[2]["commitments"].([]any)) != 2 {
		t.Fatal(bodies[2])
	}
	if name, ok := bodies[3]["name"]; !ok || name != nil {
		t.Fatal(bodies[3])
	}
}

func TestPoolQuotaIsNotRetried(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(429)
		fmt.Fprint(w, `{"error":{"code":"quota_exceeded","message":"pool quota reached","details":{"metric":"pools","limit":2}}}`)
	}))
	defer srv.Close()
	c, err := arker.New(arker.Options{APIKey: "k", BaseURL: srv.URL, Retry: fastRetry})
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.CreatePool(context.Background(), arker.CreatePoolRequest{Type: "self_hosted"})
	var apiErr *arker.Error
	if !errors.As(err, &apiErr) || apiErr.Code != "quota_exceeded" || len(apiErr.Details) == 0 || calls != 1 {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
}

func TestPoolShellRefused(t *testing.T) {
	c, err := arker.New(arker.Options{APIKey: "k", BaseURL: "https://unused.invalid"})
	if err != nil {
		t.Fatal(err)
	}
	v := c.VM("vm-1")
	id := "pool-1"
	v.Info = &arker.VMInfo{PoolID: &id}
	if _, err := v.ConnectPTY(context.Background(), arker.PTYOptions{}); err == nil {
		t.Fatal("expected pool shell refusal")
	}
}
