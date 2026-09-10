package arker

import (
	"context"
	"net/http"
)

// Session is one shell in a VM. Sessions are tabs: each keeps its own working
// directory, environment and history, and each handles one run at a time.
type Session struct {
	SessionID   string            `json:"session_id"`
	SessionIdx  int               `json:"session_idx,omitempty"`
	State       string            `json:"state,omitempty"`
	CWD         string            `json:"cwd,omitempty"`
	Env         map[string]string `json:"env,omitempty"`
	StartedAt   string            `json:"started_at,omitempty"`
	VMID        string            `json:"vm_id,omitempty"`
	VMName      string            `json:"vm_name,omitempty"`
	SourceOrgID string            `json:"source_org_id,omitempty"`
	Region      string            `json:"region,omitempty"`
	Provider    string            `json:"provider,omitempty"`
}

// CreateSessionRequest opens a session and fixes its starting environment.
type CreateSessionRequest struct {
	Env     map[string]string `json:"env,omitempty"`
	CWD     string            `json:"cwd,omitempty"`
	Command string            `json:"command,omitempty"`
	PTY     *bool             `json:"pty,omitempty"`
	Cols    *int              `json:"cols,omitempty"`
	Rows    *int              `json:"rows,omitempty"`
}

// UpdateSessionRequest resizes a session's PTY and/or sets its idle timeout.
// It works whether or not a PTY is attached -- the REST equivalent of
// PTY.Resize, which sends an in-band control frame on the live WebSocket.
type UpdateSessionRequest struct {
	Cols        *int `json:"cols,omitempty"`
	Rows        *int `json:"rows,omitempty"`
	TimeoutSecs *int `json:"timeout_secs,omitempty"`
}

// SessionList is one page of sessions.
type SessionList struct {
	Sessions   []Session `json:"sessions"`
	NextCursor string    `json:"next_cursor,omitempty"`
}

// ListSessionsOptions narrows a session listing.
type ListSessionsOptions struct {
	Cursor string
	Limit  int
	State  string
}

// ListSessions pages this VM's sessions; a 404 reads as empty.
func (v *VM) ListSessions(ctx context.Context, opts ListSessionsOptions) (*SessionList, error) {
	q := newQuery()
	q.str("cursor", opts.Cursor)
	q.num("limit", opts.Limit)
	q.str("state", opts.State)
	var out SessionList
	status, err := v.do(ctx, http.MethodGet, q.on(v.path("/sessions")), nil, &out)
	if status == http.StatusNotFound {
		return &SessionList{}, nil
	}
	return &out, err
}

// CreateSession opens a session on this VM.
func (v *VM) CreateSession(ctx context.Context, req CreateSessionRequest) (*Session, error) {
	var out Session
	_, err := v.do(ctx, http.MethodPost, v.path("/sessions"), req, &out)
	return &out, err
}

// GetSession fetches one session.
func (v *VM) GetSession(ctx context.Context, sessionID string) (*Session, error) {
	var out Session
	_, err := v.do(ctx, http.MethodGet, v.path("/sessions/"+segment(sessionID)), nil, &out)
	return &out, err
}

// UpdateSession resizes a session and/or sets its idle timeout.
func (v *VM) UpdateSession(ctx context.Context, sessionID string, req UpdateSessionRequest) error {
	_, err := v.do(ctx, http.MethodPatch, v.path("/sessions/"+segment(sessionID)), req, nil)
	return err
}

// DeleteSession closes a session. Retry-safe: an already-absent session is
// success.
func (v *VM) DeleteSession(ctx context.Context, sessionID string) error {
	status, err := v.do(ctx, http.MethodDelete, v.path("/sessions/"+segment(sessionID)), nil, nil)
	if status == http.StatusNotFound {
		return nil
	}
	return err
}
