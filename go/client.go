// Package arker is the Go client for the Arker API.
//
// Arker runs hyper-elastic, durable virtual machines for agent workloads; its
// core primitives are fork, run, and sync.
//
//	client, err := arker.New(arker.Options{APIKey: os.Getenv("ARKER_API_KEY")})
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
	defaultBaseURL = "https://api.arker.ai/api"
	defaultTimeout = 120 * time.Second

	// Matches the Python and TypeScript SDKs: four total wire attempts, not
	// four retries on top of the first.
	defaultRetryAttempts = 4
	defaultBaseDelay     = 200 * time.Millisecond
	defaultMaxDelay      = 2 * time.Second
)

// retryableStatus is the set arkerd and its gateways use to mean "try again".
//
// 429 and 503 are REFUSALS -- the origin did no work, so retrying is
// unambiguously safe. 502 and 504 are gateway errors, where the origin may
// already have acted; those are only safe to retry because Fork carries an
// Idempotency-Key (see Client.Fork).
var retryableStatus = map[int]bool{
	http.StatusTooManyRequests:    true, // 429
	http.StatusBadGateway:         true, // 502
	http.StatusServiceUnavailable: true, // 503
	http.StatusGatewayTimeout:     true, // 504
}

// Retry bounds how hard the client tries. Attempts is the TOTAL number of
// wire attempts; 1 disables retrying.
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

// New builds a Client. BaseURL defaults to production; point it at a feature
// or staging environment by setting it explicitly.
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
	retry := Retry{Attempts: defaultRetryAttempts, BaseDelay: defaultBaseDelay, MaxDelay: defaultMaxDelay}
	if opts.Retry != nil {
		retry = *opts.Retry
		if retry.Attempts < 1 {
			retry.Attempts = 1
		}
	}
	return &Client{apiKey: opts.APIKey, baseURL: base, http: httpClient, retry: retry}, nil
}

// newIdempotencyKey mints a key for one logical operation.
//
// 41 characters, inside the server's 64-character limit, and the same
// `sdk-fork-<hex>` shape the Python and TypeScript SDKs emit.
func newIdempotencyKey(prefix string) string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		// crypto/rand failing is not recoverable and not maskable: a
		// predictable key would collide across callers and replay the wrong
		// VM, which is worse than failing the call.
		panic("arker: crypto/rand unavailable: " + err.Error())
	}
	return prefix + hex.EncodeToString(buf)
}

// do performs one API call with retries.
//
// idempotencyKey is bound ONCE, before the loop, so every attempt of a single
// call presents the same key and the server replays instead of repeating the
// work.
func (c *Client) do(
	ctx context.Context,
	method, path string,
	body any,
	idempotencyKey string,
	out any,
) (int, error) {
	var payload []byte
	if body != nil {
		var err error
		if payload, err = json.Marshal(body); err != nil {
			return 0, fmt.Errorf("arker: encode request: %w", err)
		}
	}

	// A transport failure on a MUTATION leaves the outcome unknown, so it is
	// not retried -- retrying blind is how one fork becomes two VMs. Reads are
	// safe to repeat.
	isRead := method == http.MethodGet || method == http.MethodHead

	var lastErr error
	for attempt := 0; attempt < c.retry.Attempts; attempt++ {
		var reader io.Reader
		if payload != nil {
			reader = bytes.NewReader(payload)
		}
		req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
		if err != nil {
			return 0, err
		}
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if idempotencyKey != "" {
			req.Header.Set("Idempotency-Key", idempotencyKey)
		}

		resp, err := c.http.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return 0, ctx.Err()
			}
			if !isRead {
				// Do NOT retry: the server may already have acted.
				return 0, &UnknownOutcomeError{Method: method, Path: path, Err: err}
			}
			lastErr = err
			if !c.sleepBeforeRetry(ctx, attempt, nil) {
				return 0, err
			}
			continue
		}

		status, raw, err := readAll(resp)
		if err != nil {
			return status, err
		}
		if status >= http.StatusBadRequest {
			apiErr := decodeError(status, raw)
			if retryable(apiErr) && attempt < c.retry.Attempts-1 {
				lastErr = apiErr
				if !c.sleepBeforeRetry(ctx, attempt, apiErr) {
					return status, apiErr
				}
				continue
			}
			return status, apiErr
		}
		if out != nil && len(bytes.TrimSpace(raw)) > 0 {
			if err := json.Unmarshal(raw, out); err != nil {
				return status, fmt.Errorf("arker: decode response: %w", err)
			}
		}
		return status, nil
	}
	return 0, lastErr
}

func readAll(resp *http.Response) (int, []byte, error) {
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(resp.Body)
	return resp.StatusCode, raw, err
}

// retryable resolves the server's intent. An explicit `retryable` wins; the
// status is the fallback, because the field is often absent entirely.
func retryable(err *Error) bool {
	if err.Retryable != nil {
		return *err.Retryable
	}
	return retryableStatus[err.StatusCode]
}

// sleepBeforeRetry honours the server's retry_after hint over local backoff --
// it knows about capacity, the client does not. Returns false if the context
// ended first.
func (c *Client) sleepBeforeRetry(ctx context.Context, attempt int, err *Error) bool {
	delay := time.Duration(float64(c.retry.BaseDelay) * math.Pow(2, float64(attempt)))
	if delay > c.retry.MaxDelay {
		delay = c.retry.MaxDelay
	}
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
	out := &Error{StatusCode: status}
	var envelope struct {
		Error struct {
			Code       string   `json:"code"`
			Message    string   `json:"message"`
			Retryable  *bool    `json:"retryable"`
			RetryAfter *float64 `json:"retry_after"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &envelope) == nil {
		out.Code = envelope.Error.Code
		out.Message = envelope.Error.Message
		out.Retryable = envelope.Error.Retryable
		out.RetryAfter = envelope.Error.RetryAfter
	}
	if out.Message == "" {
		out.Message = strings.TrimSpace(string(raw))
	}
	if out.Code == "" {
		out.Code = "internal"
	}
	return out
}
