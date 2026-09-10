package arker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/rand/v2"
	"net/http"
	"os"
	"strings"
	"time"
)

// Retry bounds the client. Attempts is the total wire attempts; 1 disables it.
type Retry struct {
	Attempts  int
	BaseDelay time.Duration
	MaxDelay  time.Duration
	Jitter    time.Duration
}

// Options configures a Client. Only APIKey is required, and it falls back to
// ARKER_API_KEY.
//
// Placement: BaseURL fully determines routing, so Provider and Region are
// ignored when it is set. Supply either BaseURL or both Provider and Region --
// org-wide calls (ListVMs, ListRuns, ListRegions, Whoami) reach the control
// plane without placement, everything else needs it.
type Options struct {
	APIKey         string
	BaseURL        string
	ControlBaseURL string
	Provider       string
	Region         string
	HTTPClient     *http.Client
	Retry          *Retry
}

// Client talks to one Arker deployment.
type Client struct {
	apiKey     string
	baseURL    string
	controlURL string
	provider   string
	region     string
	http       *http.Client
	retry      Retry
}

// New builds a Client.
func New(opts Options) (*Client, error) {
	key := firstNonEmpty(opts.APIKey, os.Getenv("ARKER_API_KEY"))
	if key == "" {
		return nil, errors.New("arker: APIKey is required; pass APIKey or set ARKER_API_KEY")
	}
	base := firstNonEmpty(opts.BaseURL, os.Getenv("ARKER_BASE_URL"))
	provider, region := opts.Provider, opts.Region
	if base != "" {
		provider, region = "", ""
	} else {
		provider = firstNonEmpty(provider, os.Getenv("ARKER_PROVIDER"))
		region = firstNonEmpty(region, os.Getenv("ARKER_REGION"))
		if (provider == "") != (region == "") {
			return nil, errors.New("arker: Provider and Region are required together unless BaseURL is supplied")
		}
		if provider != "" {
			base = computeBaseURL(provider, region)
		}
	}
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = defaultHTTPClient()
	}
	retry := Retry{defaultAttempts, defaultBaseDelay, defaultMaxDelay, defaultJitter}
	if opts.Retry != nil {
		retry = *opts.Retry
		retry.Attempts = max(retry.Attempts, 1)
	}
	return &Client{
		apiKey:     key,
		baseURL:    trimURL(base),
		controlURL: trimURL(firstNonEmpty(opts.ControlBaseURL, os.Getenv("ARKER_CONTROL_BASE_URL"), DefaultControlBaseURL)),
		provider:   provider,
		region:     region,
		http:       httpClient,
		retry:      retry,
	}, nil
}

// BaseURL is the regional endpoint this client forks and runs against.
func (c *Client) BaseURL() (string, error) {
	if c.baseURL == "" {
		return "", errors.New("arker: no placement configured; set ARKER_PROVIDER and ARKER_REGION, or pass Options{Provider, Region} or Options{BaseURL}")
	}
	return c.baseURL, nil
}

// ControlBaseURL is the org-wide endpoint, always configured.
func (c *Client) ControlBaseURL() string { return c.controlURL }

// Provider and Region report the configured placement, empty when BaseURL was
// given directly.
func (c *Client) Provider() string { return c.provider }
func (c *Client) Region() string   { return c.region }

// call is one API request. Base empty means the client's regional endpoint.
type call struct {
	method  string
	path    string
	base    string
	body    any
	key     string // Idempotency-Key, bound once so every retry presents it
	headers map[string]string
	out     any
}

func (c *Client) get(ctx context.Context, base, path string, out any) (int, error) {
	return c.do(ctx, call{method: http.MethodGet, path: path, base: base, out: out})
}

func (c *Client) control(ctx context.Context, path string, out any) (int, error) {
	return c.get(ctx, c.controlURL, path, out)
}

// do performs one API call with retries.
func (c *Client) do(ctx context.Context, cl call) (int, error) {
	base := cl.base
	if base == "" {
		var err error
		if base, err = c.BaseURL(); err != nil {
			return 0, err
		}
	}
	var payload []byte
	if cl.body != nil {
		var err error
		if payload, err = json.Marshal(cl.body); err != nil {
			return 0, fmt.Errorf("arker: encode request: %w", err)
		}
	}
	// A transport failure on a mutation leaves the outcome unknown, so it is
	// not retried. Reads are safe to repeat.
	isRead := cl.method == http.MethodGet || cl.method == http.MethodHead

	var lastErr error
	for attempt := range c.retry.Attempts {
		resp, err := c.send(ctx, base, cl, payload)
		if err != nil {
			if ctx.Err() != nil {
				return 0, ctx.Err()
			}
			if !isRead {
				return 0, &UnknownOutcomeError{cl.method, cl.path, err}
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
			if cl.out != nil && len(bytes.TrimSpace(raw)) > 0 {
				if err := json.Unmarshal(raw, cl.out); err != nil {
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

func (c *Client) send(ctx context.Context, base string, cl call, payload []byte) (*http.Response, error) {
	var reader io.Reader
	if payload != nil {
		reader = bytes.NewReader(payload)
	}
	req, err := http.NewRequestWithContext(ctx, cl.method, base+cl.path, reader)
	if err != nil {
		return nil, err
	}
	c.auth(req.Header)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if cl.key != "" {
		req.Header.Set("Idempotency-Key", cl.key)
	}
	for k, v := range cl.headers {
		req.Header.Set(k, v)
	}
	return c.http.Do(req)
}

func (c *Client) auth(h http.Header) {
	h.Set("Authorization", "Bearer "+c.apiKey)
	h.Set("User-Agent", "arker-go/"+Version)
}

func drain(resp *http.Response) (int, []byte, error) {
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(resp.Body)
	return resp.StatusCode, raw, err
}

// retryable resolves the server's intent; the status is only the fallback,
// because `retryable` is frequently absent.
func retryable(err *Error) bool {
	switch {
	case err.Retryable != nil:
		return *err.Retryable
	case retryableStatus[err.StatusCode]:
		return true
	case retryableCodes[err.Code]:
		return true
	case err.Code != "internal":
		return false
	}
	// An `internal` can wrap a transient upstream fault; the message is the
	// only signal the service gives for those.
	for _, hint := range []string{"503", "Service Unavailable", "throttle", "SlowDown", "ThrottlingException"} {
		if strings.Contains(err.Message, hint) {
			return true
		}
	}
	return false
}

// backoff waits, preferring the server's retry_after hint -- it knows about
// capacity, the client does not. False if the context ended first.
func (c *Client) backoff(ctx context.Context, attempt int, err *Error) bool {
	delay := min(time.Duration(float64(c.retry.BaseDelay)*math.Pow(2, float64(attempt))), c.retry.MaxDelay)
	if err != nil && err.RetryAfter != nil {
		delay = time.Duration(*err.RetryAfter * float64(time.Second))
	}
	if c.retry.Jitter > 0 {
		delay += rand.N(c.retry.Jitter)
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

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

func defaultHTTPClient() *http.Client { return &http.Client{Timeout: defaultTimeout} }

func trimURL(v string) string { return strings.TrimRight(strings.TrimSpace(v), "/") }
