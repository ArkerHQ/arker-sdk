package unit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

const poolID = "7d1f7c2e-8a53-4c1e-9f3a-2b6d0c4e5f61"

const poolBody = `{"pool_id":"` + poolID + `","name":"ci","provider":"aws","region":"us-west-2",` +
	`"state":"idle","state_detail":null,"resources":{"vcpu":8,"memory_mib":32768},` +
	`"capacity":{"vcpu":0,"memory_mib":0,"disk_mib":0,"forks":null},` +
	`"commitments":[{"starts_at":"2026-10-01T00:00:00Z","ends_at":"2026-10-31T00:00:00Z"}],` +
	`"compute":{"type":"managed"},"joinable":false,"dedicated_storage":[],` +
	`"source_vm_id":null,"max_forks":null,"created_at":"2026-09-25T00:00:00Z"}`

const workerBody = `{"workers":[{"worker_id":"01J","pool_id":"` + poolID + `","kind":"blast","version":"1",` +
	`"platforms":["blast-docker-x86_64"],"state":"active",` +
	`"resources":{"vcpu":4,"memory_mib":8192,"disk_mib":1024},"free":{"vcpu":2,"memory_mib":4096,"disk_mib":512},` +
	`"last_heartbeat_at":"2026-10-12T00:00:00Z"}]}`

const apiKeyBody = `{"api_key_id":"key_1","name":"worker","role":"member","scopes":["pool:` + poolID + `:join"],` +
	`"created_at":"2026-10-12T00:00:00Z","expires_at":"2026-12-01T00:00:00Z"}`

type recorded struct {
	host, method, path, query string
	body                      map[string]any
}

// twoServers returns a client whose regional and control-plane URLs are
// different servers, so a test can tell which one a call used.
func twoServers(t *testing.T) (*arker.Client, *[]recorded) {
	t.Helper()
	var requests []recorded
	handler := func(host string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			var body map[string]any
			if len(raw) > 0 {
				_ = json.Unmarshal(raw, &body)
			}
			requests = append(requests, recorded{host, r.Method, r.URL.Path, r.URL.RawQuery, body})
			switch {
			case r.Method == http.MethodDelete:
				w.WriteHeader(http.StatusNoContent)
			case r.URL.Path == "/v1/api-keys" && r.Method == http.MethodGet:
				fmt.Fprint(w, `{"api_keys":[`+apiKeyBody+`]}`)
			case r.URL.Path == "/v1/api-keys":
				fmt.Fprint(w, `{"api_key":`+apiKeyBody+`,"key":"ark_live_secret"}`)
			case r.URL.Path == "/v1/pools" && r.Method == http.MethodGet:
				fmt.Fprint(w, `{"pools":[`+poolBody+`],"next_cursor":null}`)
			case r.URL.Path == "/v1/pools/"+poolID+"/workers":
				fmt.Fprint(w, workerBody)
			default:
				fmt.Fprint(w, poolBody)
			}
		})
	}
	regional := httptest.NewServer(handler("regional"))
	control := httptest.NewServer(handler("control"))
	t.Cleanup(regional.Close)
	t.Cleanup(control.Close)
	c, err := arker.New(arker.Options{APIKey: "k", BaseURL: regional.URL, ControlBaseURL: control.URL, Retry: fastRetry})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	return c, &requests
}

func expect(t *testing.T, got recorded, host, method, path string) {
	t.Helper()
	if got.host != host || got.method != method || got.path != path {
		t.Fatalf("got %s %s %s, want %s %s %s", got.host, got.method, got.path, host, method, path)
	}
}

func TestListAndGetPoolUseTheControlPlane(t *testing.T) {
	c, requests := twoServers(t)
	ctx := context.Background()

	list, err := c.ListPools(ctx, arker.ListPoolsOptions{Limit: 5})
	if err != nil || len(list.Pools) != 1 || list.Pools[0].PoolID != poolID {
		t.Fatalf("list %+v %v", list, err)
	}
	pool, err := c.GetPool(ctx, poolID)
	if err != nil || pool.State != "idle" || pool.Compute.Type != arker.PoolComputeManaged {
		t.Fatalf("get %+v %v", pool, err)
	}
	expect(t, (*requests)[0], "control", "GET", "/v1/pools")
	if (*requests)[0].query != "limit=5" {
		t.Fatalf("query %q", (*requests)[0].query)
	}
	expect(t, (*requests)[1], "control", "GET", "/v1/pools/"+poolID)
}

func TestCreatePoolPostsToTheRegionalBaseURL(t *testing.T) {
	c, requests := twoServers(t)
	ctx := context.Background()

	_, err := c.CreatePool(ctx, arker.CreatePoolRequest{
		Name:        "ci",
		Resources:   &arker.PoolResources{VCPU: 8, MemoryMiB: 32768},
		Commitments: []arker.PoolCommitment{{StartsAt: "2026-10-01T00:00:00Z", EndsAt: "2026-10-31T00:00:00Z"}},
		Compute: &arker.PoolCompute{
			Type: arker.PoolComputeAWS, AccountID: "123456789012",
			RoleARN: "arn:aws:iam::123456789012:role/arker", SubnetIDs: []string{"subnet-1"},
		},
		Joinable:         true,
		DedicatedStorage: []string{"object_storage"},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	_, err = c.CreatePool(ctx, arker.CreatePoolRequest{SourceVMID: "vm_1", MaxForks: 50})
	if err != nil {
		t.Fatalf("create reserve: %v", err)
	}

	expect(t, (*requests)[0], "regional", "POST", "/v1/pools")
	want := map[string]any{
		"name":        "ci",
		"resources":   map[string]any{"vcpu": 8.0, "memory_mib": 32768.0},
		"commitments": []any{map[string]any{"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T00:00:00Z"}},
		"compute": map[string]any{
			"type": "aws", "account_id": "123456789012",
			"role_arn": "arn:aws:iam::123456789012:role/arker", "subnet_ids": []any{"subnet-1"},
		},
		"joinable":          true,
		"dedicated_storage": []any{"object_storage"},
	}
	if !reflect.DeepEqual((*requests)[0].body, want) {
		t.Fatalf("body %v", (*requests)[0].body)
	}
	if !reflect.DeepEqual((*requests)[1].body, map[string]any{"source_vm_id": "vm_1", "max_forks": 50.0}) {
		t.Fatalf("reserve body %v", (*requests)[1].body)
	}
}

func TestCreatePoolWithoutARegionFailsBeforeAnyRequest(t *testing.T) {
	c, err := arker.New(arker.Options{APIKey: "k", ControlBaseURL: "http://control.invalid", Retry: fastRetry})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if _, err := c.CreatePool(context.Background(), arker.CreatePoolRequest{Name: "x"}); err == nil {
		t.Fatal("expected a missing-placement error")
	}
}

func TestUpdateDeleteAndWorkersUseTheRegionalBaseURL(t *testing.T) {
	c, requests := twoServers(t)
	ctx := context.Background()

	if _, err := c.UpdatePool(ctx, poolID, arker.UpdatePoolRequest{
		Joinable: arker.Ptr(true), Resources: &arker.PoolResources{VCPU: 16},
	}); err != nil {
		t.Fatalf("update: %v", err)
	}
	if _, err := c.UpdatePool(ctx, poolID, arker.UpdatePoolRequest{ClearName: true, Commitments: []arker.PoolCommitment{}}); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if err := c.DeletePool(ctx, poolID); err != nil {
		t.Fatalf("delete: %v", err)
	}
	workers, err := c.ListPoolWorkers(ctx, poolID)
	if err != nil || len(workers) != 1 || workers[0].Free.VCPU != 2 || workers[0].Kind != "blast" {
		t.Fatalf("workers %+v %v", workers, err)
	}

	path := "/v1/pools/" + poolID
	expect(t, (*requests)[0], "regional", "PATCH", path)
	if !reflect.DeepEqual((*requests)[0].body, map[string]any{"joinable": true, "resources": map[string]any{"vcpu": 16.0}}) {
		t.Fatalf("update body %v", (*requests)[0].body)
	}
	if !reflect.DeepEqual((*requests)[1].body, map[string]any{"name": nil, "commitments": []any{}}) {
		t.Fatalf("clear body %v", (*requests)[1].body)
	}
	expect(t, (*requests)[2], "regional", "DELETE", path)
	expect(t, (*requests)[3], "regional", "GET", path+"/workers")
}

func TestAPIKeysUseTheControlPlane(t *testing.T) {
	c, requests := twoServers(t)
	ctx := context.Background()

	keys, err := c.ListAPIKeys(ctx)
	if err != nil || len(keys) != 1 || keys[0].APIKeyID != "key_1" || keys[0].Scopes[0] != arker.ScopePoolJoin(poolID) {
		t.Fatalf("list %+v %v", keys, err)
	}
	created, err := c.CreateAPIKey(ctx, arker.CreateAPIKeyRequest{
		Name: "worker", Role: "member", Scopes: []arker.Scope{arker.ScopePoolJoin(poolID)}, ExpiresAt: "2026-12-01T00:00:00Z",
	})
	if err != nil || created.Key != "ark_live_secret" {
		t.Fatalf("create %+v %v", created, err)
	}
	if err := c.RevokeAPIKey(ctx, "key_1"); err != nil {
		t.Fatalf("revoke: %v", err)
	}

	expect(t, (*requests)[0], "control", "GET", "/v1/api-keys")
	expect(t, (*requests)[1], "control", "POST", "/v1/api-keys")
	want := map[string]any{
		"name": "worker", "role": "member", "scopes": []any{"pool:" + poolID + ":join"}, "expires_at": "2026-12-01T00:00:00Z",
	}
	if !reflect.DeepEqual((*requests)[1].body, want) {
		t.Fatalf("create body %v", (*requests)[1].body)
	}
	expect(t, (*requests)[2], "control", "DELETE", "/v1/api-keys/key_1")
}

func TestScopeBuilders(t *testing.T) {
	if arker.ScopeAll != "*" || arker.ScopeVM("vm_1") != "vmid:vm_1" ||
		arker.ScopeOp(arker.OpSync) != "op:sync" || arker.ScopePoolJoin(poolID) != arker.Scope("pool:"+poolID+":join") {
		t.Fatal("unexpected scope strings")
	}
}
