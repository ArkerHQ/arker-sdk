package unit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

const poolID = "7d1f7c2e-8a53-4c1e-9f3a-2b6d0c4e5f61"

const startedCommitment = `{"starts_at":"2026-10-01T00:00:00Z","ends_at":"2026-10-31T00:00:00Z"}`

const arkerPoolBody = `{"pool_id":"` + poolID + `","type":"arker","name":"agents","region":"us-west-2",` +
	`"platform":"graviton4","state":"ready","state_detail":null,"created_at":"2026-09-25T00:00:00Z",` +
	`"resources":{"vcpu":64,"memory_mib":131072,"disk_mib":524288},"commitments":[` + startedCommitment + `]}`

const awsPoolBody = `{"pool_id":"` + poolID + `","type":"aws","name":null,"region":"us-west-2",` +
	`"platform":"icelake","state":"provisioning","state_detail":null,"created_at":"2026-09-25T00:00:00Z",` +
	`"resources":{"vcpu":120,"memory_mib":245760,"disk_mib":1048576},"commitments":[],"isolation":["vms"],` +
	`"capacity":{"vcpu":0,"memory_mib":0,"disk_mib":0},"aws":{"account_id":"123456789012",` +
	`"subnet_ids":["subnet-0a1b2c3d"],"security_group_ids":[],"instance_types":["c6i.metal"],` +
	`"role_name":"ArkerPool-7d1f7c2e","external_id":"ext","setup_url":"https://setup.invalid"}}`

func decodeBody(t *testing.T, r *http.Request) map[string]any {
	t.Helper()
	raw, _ := io.ReadAll(r.Body)
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("request body %q: %v", raw, err)
	}
	return body
}

func TestCreatePoolGoesToTheClientsRegion(t *testing.T) {
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/pools" {
			t.Errorf("regional got %s %s", r.Method, r.URL.Path)
		}
		body = decodeBody(t, r)
		w.WriteHeader(http.StatusCreated)
		fmt.Fprint(w, awsPoolBody)
	}, reject(t, "control"))

	pool, err := c.CreatePool(context.Background(), arker.CreatePoolRequest{
		Type:      "aws",
		Platform:  "icelake",
		Resources: &arker.PoolResources{VCPU: 120, MemoryMiB: 245760, DiskMiB: 1048576},
		AWS:       &arker.CreatePoolAWS{AccountID: "123456789012", SubnetIDs: []string{"subnet-0a1b2c3d"}},
		Isolation: []string{"vms"},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if pool.AWS == nil || pool.AWS.SetupURL != "https://setup.invalid" {
		t.Fatalf("pool %+v", pool)
	}
	want := map[string]any{
		"type":      "aws",
		"platform":  "icelake",
		"resources": map[string]any{"vcpu": 120.0, "memory_mib": 245760.0, "disk_mib": 1048576.0},
		"aws":       map[string]any{"account_id": "123456789012", "subnet_ids": []any{"subnet-0a1b2c3d"}},
		"isolation": []any{"vms"},
	}
	if !reflect.DeepEqual(body, want) {
		t.Fatalf("body %v", body)
	}
}

func TestPoolCallsGoToTheControlPlane(t *testing.T) {
	base := "/v1/pools/" + poolID
	for _, tc := range []struct {
		method, path, body string
		call               func(*arker.Client) error
	}{
		{"GET", "/v1/pools", `{"pools":[` + arkerPoolBody + `],"next_cursor":null}`, func(c *arker.Client) error {
			_, err := c.ListPools(context.Background(), arker.ListPoolsOptions{})
			return err
		}},
		{"GET", base, arkerPoolBody, func(c *arker.Client) error {
			_, err := c.GetPool(context.Background(), poolID)
			return err
		}},
		{"DELETE", base, arkerPoolBody, func(c *arker.Client) error {
			_, err := c.DeletePool(context.Background(), poolID)
			return err
		}},
		{"GET", base + "/usage", `{"pool_id":"` + poolID + `","resources_allocated":{"vcpu":1,"memory_mib":2,"disk_mib":3},"allocation_observed_at":"x"}`, func(c *arker.Client) error {
			_, err := c.GetPoolUsage(context.Background(), poolID)
			return err
		}},
		{"GET", base + "/workers", `{"workers":[]}`, func(c *arker.Client) error {
			_, err := c.ListPoolWorkers(context.Background(), poolID)
			return err
		}},
		{"POST", base + "/join-keys", `{"join_key_id":"jk_1","key":"secret","created_at":"x"}`, func(c *arker.Client) error {
			_, err := c.CreatePoolJoinKey(context.Background(), poolID)
			return err
		}},
		{"GET", base + "/join-keys", `{"join_keys":[]}`, func(c *arker.Client) error {
			_, err := c.ListPoolJoinKeys(context.Background(), poolID)
			return err
		}},
		{"DELETE", base + "/join-keys/jk_1", "", func(c *arker.Client) error {
			return c.RevokePoolJoinKey(context.Background(), poolID, "jk_1")
		}},
	} {
		t.Run(tc.method+" "+tc.path, func(t *testing.T) {
			c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
				if r.Method != tc.method || r.URL.Path != tc.path {
					t.Errorf("control got %s %s", r.Method, r.URL.Path)
				}
				fmt.Fprint(w, tc.body)
			})
			if err := tc.call(c); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestUpdatePoolSendsOnlyWhatChanges(t *testing.T) {
	for _, tc := range []struct {
		name string
		req  arker.UpdatePoolRequest
		want string
	}{
		{"an empty name removes it", arker.UpdatePoolRequest{Name: arker.Ptr("")}, `{"name":null}`},
		{"zero is a size", arker.UpdatePoolRequest{Resources: &arker.PoolResources{}}, `{"resources":{"vcpu":0,"memory_mib":0,"disk_mib":0}}`},
		{"an empty list is sent", arker.UpdatePoolRequest{Commitments: &[]arker.PoolCommitment{}}, `{"commitments":[]}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var raw []byte
			c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
				raw, _ = io.ReadAll(r.Body)
				fmt.Fprint(w, arkerPoolBody)
			})
			if _, err := c.UpdatePool(context.Background(), poolID, tc.req); err != nil {
				t.Fatal(err)
			}
			if string(raw) != tc.want {
				t.Fatalf("body %s, want %s", raw, tc.want)
			}
		})
	}
}

func TestAddPoolCommitmentKeepsTheStartedOnes(t *testing.T) {
	var patched map[string]any
	c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPatch {
			patched = decodeBody(t, r)
		}
		fmt.Fprint(w, arkerPoolBody)
	})

	added := arker.PoolCommitment{StartsAt: "2026-11-01T00:00:00Z", EndsAt: "2026-12-01T00:00:00Z"}
	if _, err := c.AddPoolCommitment(context.Background(), poolID, added); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"commitments": []any{
		map[string]any{"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T00:00:00Z"},
		map[string]any{"starts_at": added.StartsAt, "ends_at": added.EndsAt},
	}}
	if !reflect.DeepEqual(patched, want) {
		t.Fatalf("patch %v", patched)
	}
}
