package unit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

func TestVMAndSessionStatesDecode(t *testing.T) {
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/vms/vm_1/sessions" {
			if got := r.URL.Query().Get("state"); got != "pending" {
				t.Errorf("state filter sent as %q", got)
			}
			fmt.Fprint(w, `{"sessions":[{"session_id":"s1","state":"pending","cwd":"/"}]}`)
			return
		}
		fmt.Fprint(w, `{"vm_id":"vm_1","state":"running","sessions":[{"session_id":"s1","state":"pending","cwd":"/"}]}`)
	}, reject(t, "control"))

	vm := c.VM("vm_1")
	if err := vm.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	if vm.Info.State != "running" || vm.Info.Sessions[0].State != "pending" {
		t.Fatalf("states did not decode: %+v", vm.Info)
	}
	list, err := vm.ListSessions(context.Background(), arker.ListSessionsOptions{State: "pending"})
	if err != nil || list.Sessions[0].State != "pending" {
		t.Fatalf("pending session listing: %+v %v", list, err)
	}
}

// The contract's policy shapes, exactly as the server sends and takes them.
const policyRules = `{"rules":{` +
	`"idle-gate":{"type":"inbound","priority":30,"match":{"ports":[8080],"vm_state":"idle"},"action":"deny"},` +
	`"web":{"type":"inbound","priority":20,"match":{"ports":[8080],"vm_state":"running"},"action":"allow"},` +
	`"llm":{"type":"outbound","priority":10,"match":{"hosts":["api.example"]},"action":{"rewrite":{"headers":{"authorization":"${secret:KEY}"}},"suspend":true}},` +
	`"legacy":{"type":"outbound","priority":5,"action":{"scaling":{"suspend":true}}},` +
	`"rest":{"type":"outbound","priority":0,"action":"suspend"},` +
	`"nap":{"type":"schedule","after_last_running_start":600,"action":"suspend"},` +
	`"ttl":{"type":"schedule","after_created":86400,"action":"delete"},` +
	`"end":{"type":"schedule","at":"2030-01-01T00:00:00Z","action":"delete"}}}`

func TestPolicyRulesRoundTripExactly(t *testing.T) {
	var doc arker.PolicyDoc
	if err := json.Unmarshal([]byte(policyRules), &doc); err != nil {
		t.Fatal(err)
	}
	llm := doc.Rules["llm"]
	if llm.ActionObject == nil || !llm.ActionObject.Suspend || llm.ActionObject.Rewrite.Headers["authorization"] != "${secret:KEY}" {
		t.Fatalf("object action did not decode: %+v", llm)
	}
	if doc.Rules["legacy"].ActionObject.Scaling == nil || !doc.Rules["legacy"].ActionObject.Scaling.Suspend {
		t.Fatalf("deprecated scaling did not decode: %+v", doc.Rules["legacy"])
	}
	if doc.Rules["idle-gate"].Match.VMState != "idle" || doc.Rules["idle-gate"].Action != "deny" {
		t.Fatalf("vm_state rule did not decode: %+v", doc.Rules["idle-gate"])
	}
	if nap := doc.Rules["nap"]; nap.AfterLastRunningStart != 600 || nap.Action != "suspend" || nap.Priority != nil {
		t.Fatalf("schedule rule did not decode: %+v", nap)
	}
	if *doc.Rules["rest"].Priority != 0 {
		t.Fatal("priority 0 was lost")
	}

	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	var want, got any
	_ = json.Unmarshal([]byte(policyRules), &want)
	_ = json.Unmarshal(out, &got)
	if !jsonEqual(want, got) {
		t.Fatalf("re-encoded policy differs:\n%s", out)
	}
}

func TestUpdateSendsRulesAndRemovals(t *testing.T) {
	var body map[string]any
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, `{"vm_id":"vm_1","state":"idle","policies":`+policyRules+`}`)
	}, reject(t, "control"))

	info, err := c.VM("vm_1").Update(context.Background(), arker.UpdateRequest{Policies: &arker.PolicyDoc{
		Rules: map[string]*arker.PolicyRule{
			"nap": {Type: "schedule", AfterLastRunningStart: 600, Action: "suspend"},
			"old": nil,
		},
	}})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"policies": map[string]any{"rules": map[string]any{
		"nap": map[string]any{"type": "schedule", "after_last_running_start": float64(600), "action": "suspend"},
		"old": nil,
	}}}
	if !jsonEqual(want, body) {
		t.Fatalf("PATCH body = %v", body)
	}
	if info.Policies.Rules["ttl"].AfterCreated != 86400 {
		t.Fatalf("VM policies did not decode: %+v", info.Policies)
	}
}

func TestEmptyRunRequestSendsNothing(t *testing.T) {
	var raw []byte
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ = io.ReadAll(r.Body)
		fmt.Fprint(w, `{"stdout":"","stdout_encoding":"utf-8","stderr":"","stderr_encoding":"utf-8","exit_code":null}`)
	}, reject(t, "control"))

	if _, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{}); err != nil {
		t.Fatal(err)
	}
	if string(raw) != "{}" {
		t.Fatalf("empty run sent %s", raw)
	}
}

func TestBackgroundRunAckIsRunning(t *testing.T) {
	c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
		fmt.Fprint(w, `{"run_id":"run_1","session_id":"s1","state":"running"}`)
	}, reject(t, "control"))

	zero := 0
	res, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Command: "sleep 60", TimeToBackground: &zero})
	if err != nil {
		t.Fatal(err)
	}
	if res.Type != "background" || res.State != "running" || res.RunID != "run_1" {
		t.Fatalf("background ack: %+v", res)
	}
}

func TestStateRefusalsSurfaceAsTypedErrors(t *testing.T) {
	invalidState := `{"error":{"code":"invalid_state","message":"The VM is not running.","timestamp":"2026-10-16T00:00:00Z",` +
		`"request_id":"req_1","request":{"kind":"matched","operation_id":"%s"},` +
		`"details":{"resource":{"kind":"vm"},"requirement":"running"}}}`
	resourceBusy := `{"error":{"code":"resource_busy","message":"A terminal is attached to this session.","timestamp":"2026-10-16T00:00:00Z",` +
		`"request_id":"req_2","request":{"kind":"matched","operation_id":"createRun"},"details":{"resource":"session"}}}`

	cases := []struct {
		name, body, code string
		call             func(*arker.Client) error
	}{
		{"signal", fmt.Sprintf(invalidState, "createRun"), "invalid_state", func(c *arker.Client) error {
			_, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Signal: "SIGINT"})
			return err
		}},
		{"fork", fmt.Sprintf(invalidState, "fork"), "invalid_state", func(c *arker.Client) error {
			_, err := c.Fork(context.Background(), arker.ForkRequest{SourceVMID: "vm_1"})
			return err
		}},
		{"pty session run", resourceBusy, "resource_busy", func(c *arker.Client) error {
			_, err := c.VM("vm_1").Run(context.Background(), arker.RunRequest{Command: "ls", SessionID: "s1"})
			return err
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			c := twoPlane(t, func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(http.StatusConflict)
				fmt.Fprint(w, tc.body)
			}, reject(t, "control"))

			err := tc.call(c)
			var apiErr *arker.Error
			if !errors.As(err, &apiErr) || !arker.IsConflict(err) || apiErr.Code != tc.code {
				t.Fatalf("err = %#v", err)
			}
			if calls != 1 {
				t.Fatalf("a %s refusal was retried: %d calls", tc.code, calls)
			}
			var details map[string]any
			_ = json.Unmarshal(apiErr.Details, &details)
			if tc.code == "invalid_state" && details["requirement"] != "running" {
				t.Fatalf("details = %s", apiErr.Details)
			}
			if tc.code == "resource_busy" && details["resource"] != "session" {
				t.Fatalf("details = %s", apiErr.Details)
			}
		})
	}
}

func jsonEqual(a, b any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return string(x) == string(y)
}
