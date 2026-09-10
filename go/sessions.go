package arker

import (
	"context"
	"net/http"
)

// Session is one shell in a VM. Arker's run interface works like a terminal:
// sessions are tabs, each keeping its own state.
type Session struct {
	SessionID  string            `json:"session_id"`
	SessionIdx int               `json:"session_idx"`
	State      string            `json:"state,omitempty"`
	Env        map[string]string `json:"env,omitempty"`
}

// CreateSessionRequest opens a new session.
type CreateSessionRequest struct {
	Env     map[string]string `json:"env,omitempty"`
	Command string            `json:"command,omitempty"`
	CWD     string            `json:"cwd,omitempty"`
}

// ListSessions returns the VM's sessions. A 404 yields an empty slice rather
// than an error, so callers can treat "gone" and "none" alike.
func (c *Client) ListSessions(ctx context.Context, vmID string) ([]Session, error) {
	var out struct {
		Sessions []Session `json:"sessions"`
	}
	status, err := c.do(ctx, http.MethodGet, "/v1/vms/"+vmID+"/sessions", nil, "", &out)
	if status == http.StatusNotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return out.Sessions, nil
}

// CreateSession opens a session on the VM.
func (c *Client) CreateSession(ctx context.Context, vmID string, req CreateSessionRequest) (*Session, error) {
	var out Session
	if _, err := c.do(ctx, http.MethodPost, "/v1/vms/"+vmID+"/sessions", req, "", &out); err != nil {
		return nil, err
	}
	return &out, nil
}
