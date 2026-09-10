package arker

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/http"
	"strings"
	"time"
)

const (
	runPollInitial     = 500 * time.Millisecond
	runPollMax         = 3 * time.Second
	runPollBackoff     = 1.5
	runPollMargin      = 30 * time.Second
	runPollMaxFailures = 10
)

// RunRequest executes a command in a VM.
type RunRequest struct {
	Command string `json:"command"`

	// SessionIdx is FIND-OR-CREATE, and omitting it means index 0 -- where a
	// plain run also lands. A run entering a session that holds a live
	// foreground process interrupts it, so anything long-lived needs its own
	// index and probes need another.
	SessionIdx *int   `json:"session_idx,omitempty"`
	SessionID  string `json:"session_id,omitempty"`

	// Timeout is the execution/kill bound in seconds -- how long the command
	// may run before the host kills it. Nil and 0 both mean unbounded. It does
	// not bound the synchronous wait; TimeToBackground does.
	Timeout *int `json:"timeout,omitempty"`
	// TimeToBackground is the HTTP sync window in seconds: how long the call
	// blocks inline before backgrounding the run and returning a pollable
	// run_id. Nil means the server default (300). Set it to 0 to get the
	// background ack back immediately and poll GetRun yourself.
	TimeToBackground *int `json:"time_to_background,omitempty"`
	// QueueingTimeout queues instead of failing fast when capacity is short.
	QueueingTimeout *int `json:"queueing_timeout,omitempty"`

	EndSymbol string     `json:"end_symbol,omitempty"`
	VCPUCount *int       `json:"vcpu_count,omitempty"`
	MemoryMiB *int       `json:"memory_mib,omitempty"`
	DiskMiB   *int       `json:"disk_mib,omitempty"`
	Acquire   []string   `json:"-"`
	Release   []string   `json:"-"`
	Signal    string     `json:"signal,omitempty"`
	Policies  *PolicyDoc `json:"policies,omitempty"`

	// IdempotencyKey asks the server to deduplicate the command. Unlike Fork it
	// is not auto-generated: a run is usually cheap to repeat and only the
	// caller knows whether theirs is. It does not make an ambiguous network
	// failure safe to retry automatically.
	IdempotencyKey string `json:"-"`
}

// wire flattens the list fields the API takes as comma-separated strings.
func (r RunRequest) wire() any {
	type alias RunRequest
	return struct {
		alias
		Acquire string `json:"acquire,omitempty"`
		Release string `json:"release,omitempty"`
	}{alias(r), strings.Join(r.Acquire, ","), strings.Join(r.Release, ",")}
}

// RunResult is a run as VM.Run returns it.
//
// A synchronous call always yields Type "completed": if the run outlives its
// sync window, Run polls it to a terminal state under the hood. Only an
// explicit TimeToBackground of 0 yields Type "background", the running ack.
type RunResult struct {
	Type      string `json:"-"` // "completed" | "background"
	RunID     string `json:"run_id,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	State     string `json:"state,omitempty"`

	// Stdout and Stderr are decoded text. StdoutBytes and StderrBytes are
	// exactly what the command wrote -- use those for output that is not text,
	// because decoding replaces undecodable bytes and cannot be undone.
	Stdout      string `json:"-"`
	Stderr      string `json:"-"`
	StdoutBytes []byte `json:"-"`
	StderrBytes []byte `json:"-"`

	// ExitCode is nil when a prompt ended the run before a completion marker
	// arrived -- expected for EndSymbol and REPL commands.
	ExitCode *int `json:"exit_code,omitempty"`
	// FailReason is the PLATFORM explaining a failed run. Stderr is the
	// program's own error output; the two are not the same.
	FailReason         string `json:"fail_reason,omitempty"`
	Dispatch           string `json:"dispatch,omitempty"`
	MemoryRequestedMiB *int   `json:"memory_requested_mib,omitempty"`
	MemoryAchievedMiB  *int   `json:"memory_achieved_mib,omitempty"`
	MemoryPartial      bool   `json:"memory_partial,omitempty"`
}

// Completed reports whether this result carries a finished run.
func (r *RunResult) Completed() bool { return r.Type == "completed" }

// runWire is the on-the-wire run shape shared by POST /runs and GET /runs/{id}:
// output arrives as strings tagged by an encoding.
type runWire struct {
	RunID              string `json:"run_id"`
	SessionID          string `json:"session_id"`
	Command            string `json:"command"`
	State              string `json:"state"`
	StartedAt          string `json:"started_at"`
	CompletedAt        string `json:"completed_at"`
	ExitCode           *int   `json:"exit_code"`
	FailReason         string `json:"fail_reason"`
	Stdout             string `json:"stdout"`
	StdoutEncoding     string `json:"stdout_encoding"`
	Stderr             string `json:"stderr"`
	StderrEncoding     string `json:"stderr_encoding"`
	RetryCount         int    `json:"retry_count"`
	Dispatch           string `json:"dispatch"`
	VMID               string `json:"vm_id"`
	MemoryRequestedMiB *int   `json:"memory_requested_mib"`
	MemoryAchievedMiB  *int   `json:"memory_achieved_mib"`
	MemoryPartial      bool   `json:"memory_partial"`
}

func decodeOutput(text, encoding string) []byte {
	if encoding == "base64" {
		if raw, err := base64.StdEncoding.DecodeString(text); err == nil {
			return raw
		}
	}
	return []byte(text)
}

func (w runWire) result() *RunResult {
	out := &RunResult{
		Type: "completed", RunID: w.RunID, SessionID: w.SessionID, State: w.State,
		ExitCode: w.ExitCode, FailReason: w.FailReason, Dispatch: w.Dispatch,
		MemoryRequestedMiB: w.MemoryRequestedMiB, MemoryAchievedMiB: w.MemoryAchievedMiB,
		MemoryPartial: w.MemoryPartial,
	}
	out.StdoutBytes = decodeOutput(w.Stdout, w.StdoutEncoding)
	out.StderrBytes = decodeOutput(w.Stderr, w.StderrEncoding)
	out.Stdout, out.Stderr = string(out.StdoutBytes), string(out.StderrBytes)
	// A background ack carries a run_id and a state and no output at all.
	if w.State == "running" && w.ExitCode == nil && w.Stdout == "" && w.StdoutEncoding == "" {
		out.Type = "background"
	}
	if out.State == "" {
		out.State = "completed"
	}
	return out
}

// RunRecord is a fetched run, with its output decoded.
type RunRecord struct {
	RunResult
	Command     string `json:"command,omitempty"`
	StartedAt   string `json:"started_at,omitempty"`
	CompletedAt string `json:"completed_at,omitempty"`
	RetryCount  int    `json:"retry_count,omitempty"`
	VMID        string `json:"vm_id,omitempty"`
}

// Run executes a command in this VM, restoring it first if it is suspended --
// there is no wake endpoint and none is needed, a no-op run IS a wake.
//
// Synchronous by default. If the run outlives the server's sync window the API
// returns a background ack, and Run then polls GetRun until the run is terminal
// so a synchronous caller always receives the final result. That poll is
// bounded by Timeout plus a margin; exceeding it returns an error with code
// "timeout" while the run keeps executing server-side.
func (v *VM) Run(ctx context.Context, req RunRequest) (*RunResult, error) {
	var wire runWire
	_, err := v.client.do(ctx, call{
		method: http.MethodPost, path: v.path("/runs"), base: v.baseURL,
		body: req.wire(), key: strings.TrimSpace(req.IdempotencyKey), out: &wire,
	})
	if err != nil {
		return nil, err
	}
	result := wire.result()
	// The server backgrounds a run that outlived its sync window. Poll it to a
	// terminal state unless the caller explicitly asked to background.
	if result.Type == "background" && !(req.TimeToBackground != nil && *req.TimeToBackground == 0) {
		return v.awaitRun(ctx, result.RunID, req.Timeout)
	}
	return result, nil
}

// awaitRun polls until the run is terminal. An unset or zero Timeout is
// unbounded server-side, so the poll is unbounded with it: giving up at a
// client-side deadline the caller never asked for would abandon a live run.
func (v *VM) awaitRun(ctx context.Context, runID string, timeout *int) (*RunResult, error) {
	var deadline time.Time
	if timeout != nil && *timeout > 0 {
		deadline = time.Now().Add(time.Duration(*timeout)*time.Second + runPollMargin)
	}
	delay, failures := runPollInitial, 0
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(delay):
		}
		record, err := v.GetRun(ctx, runID)
		if err != nil {
			// Any answered check resets the counter, so a long command and a
			// transient blip both survive; only a service that has stopped
			// responding ends the wait.
			if failures++; failures >= runPollMaxFailures {
				return nil, fmt.Errorf("arker: run %s: %d consecutive poll failures (last: %w); it may still be going server-side -- poll GetRun to retrieve it", runID, failures, err)
			}
		} else {
			failures = 0
			if terminalRunStates[record.State] {
				return &record.RunResult, nil
			}
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			return nil, &Error{Code: "timeout", StatusCode: 0, Message: fmt.Sprintf(
				"run %s did not reach a terminal state in time; it continues server-side -- poll GetRun to retrieve it", runID)}
		}
		delay = min(time.Duration(float64(delay)*runPollBackoff), runPollMax)
	}
}

// GetRun fetches a past or in-flight run. Output grows while a run is still
// going, so a long task can be followed live rather than read only at the end.
func (v *VM) GetRun(ctx context.Context, runID string) (*RunRecord, error) {
	var wire runWire
	if _, err := v.do(ctx, http.MethodGet, v.path("/runs/"+segment(runID)), nil, &wire); err != nil {
		return nil, err
	}
	return &RunRecord{
		RunResult: *wire.result(), Command: wire.Command, StartedAt: wire.StartedAt,
		CompletedAt: wire.CompletedAt, RetryCount: wire.RetryCount, VMID: wire.VMID,
	}, nil
}

// CancelRun stops a run.
func (v *VM) CancelRun(ctx context.Context, runID string) (bool, error) {
	var out struct {
		Cancelled bool `json:"cancelled"`
	}
	_, err := v.do(ctx, http.MethodDelete, v.path("/runs/"+segment(runID)), nil, &out)
	return out.Cancelled, err
}

// RunSummary is one row of a run listing: metadata without the output.
type RunSummary struct {
	RunID       string `json:"run_id"`
	SessionID   string `json:"session_id,omitempty"`
	Command     string `json:"command,omitempty"`
	State       string `json:"state"`
	StartedAt   string `json:"started_at,omitempty"`
	CompletedAt string `json:"completed_at,omitempty"`
	ExitCode    *int   `json:"exit_code,omitempty"`
	FailReason  string `json:"fail_reason,omitempty"`
	VMID        string `json:"vm_id,omitempty"`
	VMName      string `json:"vm_name,omitempty"`
	SourceOrgID string `json:"source_org_id,omitempty"`
	Region      string `json:"region,omitempty"`
	Provider    string `json:"provider,omitempty"`
}

// RunList is one page of runs.
type RunList struct {
	Runs       []RunSummary `json:"runs"`
	NextCursor string       `json:"next_cursor,omitempty"`
}

// ListRunsOptions narrows a VM's run listing.
type ListRunsOptions struct {
	Cursor         string
	Limit          int
	State          string
	StartedAfter   string
	StartedBefore  string
	CompletedAfter string
}

// ListRuns pages this VM's runs.
func (v *VM) ListRuns(ctx context.Context, opts ListRunsOptions) (*RunList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	q.str("state", opts.State)
	q.str("started_after", opts.StartedAfter)
	q.str("started_before", opts.StartedBefore)
	q.str("completed_after", opts.CompletedAfter)
	var out RunList
	_, err := v.do(ctx, http.MethodGet, q.on(v.path("/runs")), nil, &out)
	return &out, err
}

// ListOrgRunsOptions narrows the org-wide run listing.
type ListOrgRunsOptions struct {
	Since     *int
	Until     *int
	VM        string
	VMIDs     []string
	Region    string
	Provider  string
	Search    string
	Limit     int
	Offset    int
	Lite      *bool
	Runtime   string
	Endpoint  string
	Actions   []string
	Status    []string
	StatusMin *int
	StatusMax *int
	Sort      string
	Dir       string
}

// ListRuns lists run activity across every VM in the org, through the control
// plane.
func (c *Client) ListRuns(ctx context.Context, opts ListOrgRunsOptions) (*RunList, error) {
	q := newQuery()
	q.numPtr("since", opts.Since)
	q.numPtr("until", opts.Until)
	q.str("vm", opts.VM)
	q.csv("vms", opts.VMIDs)
	q.str("region", opts.Region)
	q.str("provider", opts.Provider)
	q.str("search", opts.Search)
	q.num("limit", opts.Limit)
	q.num("offset", opts.Offset)
	q.boolPtr("lite", opts.Lite)
	q.str("runtime", opts.Runtime)
	q.str("endpoint", opts.Endpoint)
	q.csv("actions", opts.Actions)
	q.csv("status", opts.Status)
	q.numPtr("status_min", opts.StatusMin)
	q.numPtr("status_max", opts.StatusMax)
	q.str("sort", opts.Sort)
	q.str("dir", opts.Dir)
	var out RunList
	_, err := c.control(ctx, q.on("/v1/runs"), &out)
	return &out, err
}
