package arker

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/coder/websocket"
)

const ptyConnectTimeout = 30 * time.Second

// PTYOptions configures ConnectPTY.
type PTYOptions struct {
	// SessionID reattaches to an existing session, replaying its scrollback.
	// Empty creates one.
	SessionID     string
	Cols, Rows    *int
	Command       string
	Persist       *bool
	CancelTTLSecs *int

	// Env is applied when a session is created; it wins over the plain
	// defaults, so a caller can override just TERM and keep the rest. Ignored
	// when reattaching, because the session's environment is already fixed.
	Env map[string]string
	// Plain sends PlainPTYEnv. Nil means true. Set it to false when a human is
	// watching a full-screen TUI.
	Plain *bool
	// UseTicket mints a short-lived PTY ticket instead of putting the API key
	// on the upgrade request. Nil means true.
	UseTicket *bool

	OnData  func([]byte)
	OnClose func(PTYCloseEvent)
	OnError func(error)
}

// PTYCloseEvent reports why a PTY ended.
type PTYCloseEvent struct {
	Code   int
	Reason string
}

// PTY is an interactive pseudo-terminal over a WebSocket. Server output is
// delivered to OnData from a background reader.
type PTY struct {
	SessionID string

	conn   *websocket.Conn
	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{}
	once   sync.Once

	mu      sync.Mutex
	writeMu sync.Mutex
	err     error
	onData  []func([]byte)
	onClose []func(PTYCloseEvent)
	onError []func(error)
}

// ConnectPTY opens an interactive terminal in this VM.
//
// The connection outlives ctx: ctx bounds the dial only, so cancelling it after
// ConnectPTY returns does not tear the terminal down. Use Close for that.
func (v *VM) ConnectPTY(ctx context.Context, opts PTYOptions) (*PTY, error) {
	sessionID := opts.SessionID
	if sessionID == "" {
		env := map[string]string{}
		if opts.Plain == nil || *opts.Plain {
			for k, val := range PlainPTYEnv {
				env[k] = val
			}
		}
		for k, val := range opts.Env {
			env[k] = val
		}
		session, err := v.CreateSession(ctx, CreateSessionRequest{Env: env})
		if err != nil {
			return nil, err
		}
		sessionID = session.SessionID
	}

	q := newQuery()
	q.numPtr("cols", clampDim(opts.Cols))
	q.numPtr("rows", clampDim(opts.Rows))
	q.str("command", opts.Command)
	q.boolPtr("persist", opts.Persist)
	if opts.CancelTTLSecs != nil && *opts.CancelTTLSecs > 0 {
		q.num("cancel_ttl_secs", *opts.CancelTTLSecs)
	}

	header := http.Header{}
	if opts.UseTicket == nil || *opts.UseTicket {
		var ticket struct {
			Ticket string `json:"ticket"`
		}
		if _, err := v.do(ctx, http.MethodPost,
			v.path("/sessions/"+segment(sessionID)+"/pty-ticket"), map[string]any{}, &ticket); err != nil {
			return nil, err
		}
		q.str("ticket", ticket.Ticket)
	} else {
		v.client.auth(header)
	}

	wsURL, err := ptyWebSocketURL(v.baseURL, v.ID, sessionID, q)
	if err != nil {
		return nil, err
	}

	dialCtx, cancelDial := context.WithTimeout(ctx, ptyConnectTimeout)
	defer cancelDial()
	conn, _, err := websocket.Dial(dialCtx, wsURL, &websocket.DialOptions{HTTPHeader: header})
	if err != nil {
		return nil, &Error{Code: "unavailable", Message: "PTY WebSocket failed to open: " + err.Error()}
	}
	conn.SetReadLimit(-1)

	// Background, not ctx: the terminal is meant to outlive the call that
	// opened it.
	connCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	p := &PTY{SessionID: sessionID, conn: conn, ctx: connCtx, cancel: cancel, done: make(chan struct{})}
	if opts.OnData != nil {
		p.onData = append(p.onData, opts.OnData)
	}
	if opts.OnClose != nil {
		p.onClose = append(p.onClose, opts.OnClose)
	}
	if opts.OnError != nil {
		p.onError = append(p.onError, opts.OnError)
	}
	go p.read()
	return p, nil
}

// OnData registers an extra output listener and returns a function removing it.
func (p *PTY) OnData(fn func([]byte)) func() { return register(p, &p.onData, fn) }

// OnClose registers a close listener and returns a function removing it.
func (p *PTY) OnClose(fn func(PTYCloseEvent)) func() { return register(p, &p.onClose, fn) }

// OnError registers an error listener and returns a function removing it.
func (p *PTY) OnError(fn func(error)) func() { return register(p, &p.onError, fn) }

func register[T any](p *PTY, list *[]T, fn T) func() {
	p.mu.Lock()
	defer p.mu.Unlock()
	*list = append(*list, fn)
	index := len(*list) - 1
	return func() {
		p.mu.Lock()
		defer p.mu.Unlock()
		var zero T
		(*list)[index] = zero
	}
}

// Send writes stdin to the terminal.
func (p *PTY) Send(data []byte) error { return p.write(websocket.MessageBinary, data) }

// SendString writes stdin as UTF-8.
func (p *PTY) SendString(s string) error { return p.Send([]byte(s)) }

// Resize changes the terminal dimensions in band. UpdateSession does the same
// over REST when no PTY is attached.
func (p *PTY) Resize(cols, rows int) error {
	return p.control(map[string]any{"type": "resize", "cols": clamp(cols), "rows": clamp(rows)})
}

// Kill destroys the shell.
func (p *PTY) Kill() error { return p.control(map[string]any{"type": "kill"}) }

// Ping keeps the connection warm.
func (p *PTY) Ping() error { return p.control(map[string]any{"type": "ping"}) }

// Close detaches. With Persist the shell keeps running and can be reattached
// through the same SessionID.
func (p *PTY) Close() error {
	err := p.conn.Close(websocket.StatusNormalClosure, "")
	p.finish(PTYCloseEvent{Code: int(websocket.StatusNormalClosure)})
	return err
}

// Done closes when the terminal ends, from either side.
func (p *PTY) Done() <-chan struct{} { return p.done }

// Err reports why the terminal ended; nil for a clean close.
func (p *PTY) Err() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.err
}

func (p *PTY) control(message map[string]any) error {
	payload, err := json.Marshal(message)
	if err != nil {
		return err
	}
	return p.write(websocket.MessageText, payload)
}

// write serialises sends: a WebSocket connection allows one writer at a time.
func (p *PTY) write(kind websocket.MessageType, payload []byte) error {
	p.writeMu.Lock()
	defer p.writeMu.Unlock()
	return p.conn.Write(p.ctx, kind, payload)
}

func (p *PTY) read() {
	for {
		_, data, err := p.conn.Read(p.ctx)
		if err != nil {
			event := PTYCloseEvent{Code: int(websocket.CloseStatus(err))}
			var closeErr websocket.CloseError
			if errors.As(err, &closeErr) {
				event.Reason = closeErr.Reason
			} else if p.ctx.Err() == nil {
				p.fail(err)
			}
			p.finish(event)
			return
		}
		p.mu.Lock()
		listeners := append([]func([]byte){}, p.onData...)
		p.mu.Unlock()
		for _, fn := range listeners {
			if fn != nil {
				fn(data)
			}
		}
	}
}

func (p *PTY) fail(err error) {
	p.mu.Lock()
	if p.err == nil {
		p.err = err
	}
	listeners := append([]func(error){}, p.onError...)
	p.mu.Unlock()
	for _, fn := range listeners {
		if fn != nil {
			fn(err)
		}
	}
}

func (p *PTY) finish(event PTYCloseEvent) {
	p.once.Do(func() {
		p.cancel()
		p.mu.Lock()
		listeners := append([]func(PTYCloseEvent){}, p.onClose...)
		p.mu.Unlock()
		for _, fn := range listeners {
			if fn != nil {
				fn(event)
			}
		}
		close(p.done)
	})
}

func ptyWebSocketURL(baseURL, vmID, sessionID string, q *query) (string, error) {
	parsed, err := url.Parse(baseURL + vmPath(vmID, "/sessions/"+segment(sessionID)+"/pty"))
	if err != nil {
		return "", err
	}
	switch parsed.Scheme {
	case "https":
		parsed.Scheme = "wss"
	case "http":
		parsed.Scheme = "ws"
	default:
		return "", errors.New("arker: unsupported PTY WebSocket scheme: " + parsed.Scheme)
	}
	parsed.RawQuery = q.values.Encode()
	return parsed.String(), nil
}

func clamp(v int) int { return max(1, min(1000, v)) }

func clampDim(v *int) *int {
	if v == nil {
		return nil
	}
	return Ptr(clamp(*v))
}
