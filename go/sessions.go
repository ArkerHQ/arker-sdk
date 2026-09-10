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

// ListSessions returns this VM's sessions. A 404 yields an empty slice rather
// than an error, so callers can treat "gone" and "none" alike.
func (v *VM) ListSessions(ctx context.Context) ([]Session, error) {
	var out struct {
		Sessions []Session `json:"sessions"`
	}
	status, err := v.client.do(ctx, http.MethodGet, "/v1/vms/"+v.ID+"/sessions", nil, "", &out)
	if status == http.StatusNotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return out.Sessions, nil
}

// CreateSession opens a session on this VM.
func (v *VM) CreateSession(ctx context.Context, req CreateSessionRequest) (*Session, error) {
	var out Session
	if _, err := v.client.do(ctx, http.MethodPost, "/v1/vms/"+v.ID+"/sessions", req, "", &out); err != nil {
		return nil, err
	}
	return &out, nil
}
