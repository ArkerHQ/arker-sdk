package unit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

const apiKeyPoolID = "0b6c1d2e-3f40-4a51-8b62-7c83d94ea5f6"

type apiKeyCall struct {
	method, path, body string
}

// apiKeyClient points the region at a dead URL so every call must use the
// control plane.
func apiKeyClient(t *testing.T, status int, response string) (*arker.Client, *[]apiKeyCall) {
	t.Helper()
	calls := &[]apiKeyCall{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		*calls = append(*calls, apiKeyCall{r.Method, r.URL.EscapedPath(), string(body)})
		w.WriteHeader(status)
		fmt.Fprint(w, response)
	}))
	t.Cleanup(srv.Close)
	c, err := arker.New(arker.Options{APIKey: "k", BaseURL: "http://region.invalid", ControlBaseURL: srv.URL, Retry: fastRetry})
	if err != nil {
		t.Fatal(err)
	}
	return c, calls
}

func TestCreateAPIKeySendsScopes(t *testing.T) {
	cases := []struct {
		req  arker.CreateAPIKeyRequest
		want string
	}{
		{arker.CreateAPIKeyRequest{Name: "agents", Scopes: []arker.APIKeyScope{{PoolID: apiKeyPoolID}}}, `{"name":"agents","scopes":[{"pool_id":"` + apiKeyPoolID + `"}]}`},
		{arker.CreateAPIKeyRequest{Name: "agent", Scopes: []arker.APIKeyScope{{VMID: "vm_a"}, {VMID: "vm_b"}}}, `{"name":"agent","scopes":[{"vm_id":"vm_a"},{"vm_id":"vm_b"}]}`},
		{arker.CreateAPIKeyRequest{}, `{}`},
	}
	for _, tc := range cases {
		c, calls := apiKeyClient(t, 201, `{"api_key_id":"agents_abc","scopes":[{"pool_id":"`+apiKeyPoolID+`"}],"created_at":"2026-10-17T00:00:00.000Z","key":"ark_live_secret"}`)
		key, err := c.CreateAPIKey(context.Background(), tc.req)
		if err != nil {
			t.Fatal(err)
		}
		if want := []apiKeyCall{{"POST", "/v1/api-keys", tc.want}}; !reflect.DeepEqual(*calls, want) {
			t.Fatalf("calls = %+v, want %+v", *calls, want)
		}
		want := arker.CreatedAPIKey{
			APIKey: arker.APIKey{APIKeyID: "agents_abc", Scopes: []arker.APIKeyScope{{PoolID: apiKeyPoolID}}, CreatedAt: "2026-10-17T00:00:00.000Z"},
			Key:    "ark_live_secret",
		}
		if !reflect.DeepEqual(*key, want) {
			t.Fatalf("key = %+v", *key)
		}
	}
}

func TestListAPIKeysDecodes(t *testing.T) {
	c, calls := apiKeyClient(t, 200, `{"api_keys":[{"api_key_id":"org_1","scopes":[],"created_at":"t"},{"api_key_id":"vms_1","scopes":[{"vm_id":"vm_a"}],"created_at":"t"}]}`)
	list, err := c.ListAPIKeys(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if want := []apiKeyCall{{"GET", "/v1/api-keys", ""}}; !reflect.DeepEqual(*calls, want) {
		t.Fatalf("calls = %+v", *calls)
	}
	want := []arker.APIKey{
		{APIKeyID: "org_1", Scopes: []arker.APIKeyScope{}, CreatedAt: "t"},
		{APIKeyID: "vms_1", Scopes: []arker.APIKeyScope{{VMID: "vm_a"}}, CreatedAt: "t"},
	}
	if !reflect.DeepEqual(list.APIKeys, want) {
		t.Fatalf("keys = %+v", list.APIKeys)
	}
}

func TestRevokeAPIKeyDeletesTheEscapedID(t *testing.T) {
	c, calls := apiKeyClient(t, 204, "")
	if err := c.RevokeAPIKey(context.Background(), "agents/abc"); err != nil {
		t.Fatal(err)
	}
	if want := []apiKeyCall{{"DELETE", "/v1/api-keys/agents%2Fabc", ""}}; !reflect.DeepEqual(*calls, want) {
		t.Fatalf("calls = %+v", *calls)
	}
}

func TestAPIKeyRefusalsAreTyped(t *testing.T) {
	const shape = `scopes must be 1..16 {\"vm_id\"} objects or one {\"pool_id\"}`
	cases := []struct {
		status  int
		code    string
		details string
	}{
		{400, "bad_request", `{"violations":[{"field":"request","message":"` + shape + `"}]}`},
		{403, "forbidden", `null`},
		{404, "not_found", `{"resource":"pool"}`},
	}
	for _, tc := range cases {
		c, calls := apiKeyClient(t, tc.status, `{"error":{"code":"`+tc.code+`","message":"refused","details":`+tc.details+`}}`)
		_, err := c.CreateAPIKey(context.Background(), arker.CreateAPIKeyRequest{Scopes: []arker.APIKeyScope{{PoolID: apiKeyPoolID}, {VMID: "vm_a"}}})
		var apiErr *arker.Error
		if !errors.As(err, &apiErr) || apiErr.StatusCode != tc.status || apiErr.Code != tc.code {
			t.Fatalf("err = %v", err)
		}
		if len(*calls) != 1 {
			t.Fatalf("%s was retried: %d calls", tc.code, len(*calls))
		}
		if tc.code == "bad_request" {
			var details struct {
				Violations []struct{ Field, Message string }
			}
			if err := json.Unmarshal(apiErr.Details, &details); err != nil || details.Violations[0].Message != `scopes must be 1..16 {"vm_id"} objects or one {"pool_id"}` {
				t.Fatalf("details = %s (%v)", apiErr.Details, err)
			}
		}
	}
}
