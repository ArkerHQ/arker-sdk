// Package arker is the Go client for the Arker API.
//
//	client, _ := arker.New(arker.Options{APIKey: os.Getenv("ARKER_API_KEY")})
//	vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base"})
package arker

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"
)

const (
	defaultBaseURL       = "https://api.arker.ai/api"
	defaultTimeout       = 120 * time.Second
	defaultRetryAttempts = 4 // TOTAL wire attempts, not four on top of the first
	defaultBaseDelay     = 200 * time.Millisecond
	defaultMaxDelay      = 2 * time.Second
)

// 429 and 503 are refusals -- the origin did no work. 502 and 504 are gateway
// errors where it may already have acted, and are only safe to retry because
// Fork carries an Idempotency-Key.
var retryableStatus = map[int]bool{429: true, 502: true, 503: true, 504: true}

// Retry bounds the client. Attempts is the total wire attempts; 1 disables it.
type Retry struct {
	Attempts  int
	BaseDelay time.Duration
	MaxDelay  time.Duration
}

// Options configures a Client. Only APIKey is required.
type Options struct {
	APIKey     string
	BaseURL    string
	HTTPClient *http.Client
	Retry      *Retry
}

// Client talks to one Arker deployment.
type Client struct {
	apiKey  string
	baseURL string
	http    *http.Client
	retry   Retry
}

// New builds a Client. BaseURL defaults to production.
func New(opts Options) (*Client, error) {
	if strings.TrimSpace(opts.APIKey) == "" {
		return nil, fmt.Errorf("arker: APIKey is required")
	}
	base := strings.TrimRight(strings.TrimSpace(opts.BaseURL), "/")
	if base == "" {
		base = defaultBaseURL
	}
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: defaultTimeout}
	}
	retry := Retry{defaultRetryAttempts, defaultBaseDelay, defaultMaxDelay}
	if opts.Retry != nil {
		retry = *opts.Retry
		retry.Attempts = max(retry.Attempts, 1)
	}
	return &Client{opts.APIKey, base, httpClient, retry}, nil
}

// newIdempotencyKey mints a key for one logical operation: 41 chars, inside
// the server's 64 limit, same shape as the Python and TypeScript SDKs.
func newIdempotencyKey(prefix string) string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		// A predictable key would replay the wrong VM across callers, which is
		// worse than failing the call.
		panic("arker: crypto/rand unavailable: " + err.Error())
	}
	return prefix + hex.EncodeToString(buf)
}

// do performs one API call with retries. idempotencyKey is bound once, before
// the loop, so every attempt presents the same key and the server replays
// instead of repeating the work.
func (c *Client) do(ctx context.Context, method, path string, body any, idempotencyKey string, out any) (int, error) {
	var payload []byte
	if body != nil {
		var err error
		if payload, err = json.Marshal(body); err != nil {
			return 0, fmt.Errorf("arker: encode request: %w", err)
		}
	}
	// A transport failure on a mutation leaves the outcome unknown, so it is
	// not retried. Reads are safe to repeat.
	isRead := method == http.MethodGet || method == http.MethodHead

	var lastErr error
	for attempt := range c.retry.Attempts {
		resp, err := c.send(ctx, method, path, payload, idempotencyKey)
		if err != nil {
			if ctx.Err() != nil {
				return 0, ctx.Err()
			}
			if !isRead {
				return 0, &UnknownOutcomeError{method, path, err}
			}
			lastErr = err
			if !c.backoff(ctx, attempt, nil) {
				return 0, err
			}
			continue
		}

		status, raw, err := drain(resp)
		if err != nil {
			return status, err
		}
		if status < http.StatusBadRequest {
			if out != nil && len(bytes.TrimSpace(raw)) > 0 {
				if err := json.Unmarshal(raw, out); err != nil {
					return status, fmt.Errorf("arker: decode response: %w", err)
				}
			}
			return status, nil
		}

		apiErr := decodeError(status, raw)
		if !retryable(apiErr) || attempt == c.retry.Attempts-1 || !c.backoff(ctx, attempt, apiErr) {
			return status, apiErr
		}
		lastErr = apiErr
	}
	return 0, lastErr
}

func (c *Client) send(ctx context.Context, method, path string, payload []byte, key string) (*http.Response, error) {
	var reader io.Reader
	if payload != nil {
		reader = bytes.NewReader(payload)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	return c.http.Do(req)
}

func drain(resp *http.Response) (int, []byte, error) {
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(resp.Body)
	return resp.StatusCode, raw, err
}

// retryable resolves the server's intent; the status is only the fallback,
// because `retryable` is frequently absent.
func retryable(err *Error) bool {
	if err.Retryable != nil {
		return *err.Retryable
	}
	return retryableStatus[err.StatusCode]
}

// backoff waits, preferring the server's retry_after hint -- it knows about
// capacity, the client does not. False if the context ended first.
func (c *Client) backoff(ctx context.Context, attempt int, err *Error) bool {
	delay := min(time.Duration(float64(c.retry.BaseDelay)*math.Pow(2, float64(attempt))), c.retry.MaxDelay)
	if err != nil && err.RetryAfter != nil {
		delay = time.Duration(*err.RetryAfter * float64(time.Second))
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(delay):
		return true
	}
}

func decodeError(status int, raw []byte) *Error {
	out := &Error{StatusCode: status, Code: "internal"}
	var envelope struct {
		Error struct {
			Code       string   `json:"code"`
			Message    string   `json:"message"`
			Retryable  *bool    `json:"retryable"`
			RetryAfter *float64 `json:"retry_after"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &envelope) == nil && envelope.Error.Code != "" {
		out.Code = envelope.Error.Code
		out.Message = envelope.Error.Message
		out.Retryable = envelope.Error.Retryable
		out.RetryAfter = envelope.Error.RetryAfter
	}
	if out.Message == "" {
		out.Message = strings.TrimSpace(string(raw))
	}
	return out
}
