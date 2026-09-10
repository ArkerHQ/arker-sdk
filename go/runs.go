package arker

import (
	"context"
	"net/http"
	"strings"
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

	Timeout          *int `json:"timeout,omitempty"`
	TimeToBackground *int `json:"time_to_background,omitempty"`
	QueueingTimeout  *int `json:"queueing_timeout,omitempty"`

	// IdempotencyKey deduplicates the run. Unlike Fork it is not
	// auto-generated: a run is usually cheap to repeat and only the caller
	// knows whether theirs is.
	IdempotencyKey string `json:"-"`
}

// RunResult is a completed or backgrounded run. ExitCode is nil until terminal.
type RunResult struct {
	RunID     string `json:"run_id,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	Stdout    string `json:"stdout,omitempty"`
	Stderr    string `json:"stderr,omitempty"`
	ExitCode  *int   `json:"exit_code,omitempty"`
	State     string `json:"state,omitempty"`
}

// Run executes a command, restoring the VM first if it is suspended. There is
// no wake endpoint and none is needed: a no-op run IS a wake.
func (v *VM) Run(ctx context.Context, req RunRequest) (*RunResult, error) {
	var out RunResult
	_, err := v.client.do(ctx, http.MethodPost, v.path("/runs"), req, strings.TrimSpace(req.IdempotencyKey), &out)
	return &out, err
}
