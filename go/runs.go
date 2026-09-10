package arker

import (
	"context"
	"net/http"
	"strings"
)

// RunRequest executes a command in a VM.
type RunRequest struct {
	Command string `json:"command"`

	// SessionIdx selects the session. It is FIND-OR-CREATE, and omitting it
	// means index 0 -- which is also where a plain run lands. A run entering a
	// session that holds a live foreground process INTERRUPTS it, so anything
	// long-lived belongs on its own explicit index, and probes belong on
	// another.
	SessionIdx *int   `json:"session_idx,omitempty"`
	SessionID  string `json:"session_id,omitempty"`

	// Timeout in seconds. Omitted means no limit.
	Timeout *int `json:"timeout,omitempty"`
	// TimeToBackground is the sync window; 0 returns a pollable run as soon as
	// it is dispatched.
	TimeToBackground *int `json:"time_to_background,omitempty"`
	QueueingTimeout  *int `json:"queueing_timeout,omitempty"`

	// IdempotencyKey deduplicates the run server-side. Unlike Fork this is NOT
	// auto-generated: a run is usually cheap to repeat and the caller knows
	// whether theirs is.
	IdempotencyKey string `json:"-"`
}

// RunResult is a completed or backgrounded run.
type RunResult struct {
	RunID     string `json:"run_id,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	Stdout    string `json:"stdout,omitempty"`
	Stderr    string `json:"stderr,omitempty"`
	// ExitCode is nil for a run that has not reached a terminal state.
	ExitCode *int   `json:"exit_code,omitempty"`
	State    string `json:"state,omitempty"`
}

// Run executes a command in the VM, restoring it first if it is suspended.
//
// There is no separate wake endpoint: a suspended VM restores lazily on its
// next run, so a no-op run IS a wake.
func (c *Client) Run(ctx context.Context, vmID string, req RunRequest) (*RunResult, error) {
	var out RunResult
	key := strings.TrimSpace(req.IdempotencyKey)
	if _, err := c.do(ctx, http.MethodPost, "/v1/vms/"+vmID+"/runs", req, key, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
