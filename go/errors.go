package arker

import (
	"errors"
	"fmt"
	"net/http"
)

// Error is an arkerd API failure. The envelope is flat, and `retryable` is
// often absent -- absent means unspecified, not false. Branch on Code, never
// on Message.
type Error struct {
	Code       string
	Message    string
	StatusCode int
	Retryable  *bool
	RetryAfter *float64
}

func (e *Error) Error() string {
	return fmt.Sprintf("arker api status %d: %s: %s", e.StatusCode, e.Code, e.Message)
}

func statusIs(err error, status int) bool {
	var apiErr *Error
	return errors.As(err, &apiErr) && apiErr.StatusCode == status
}

// IsNotFound reports a 404, which is ORG-SCOPED: another org's resource is
// indistinguishable from one that never existed.
func IsNotFound(err error) bool { return statusIs(err, http.StatusNotFound) }

// IsConflict reports a 409 -- for a fork, a key reused for a different request.
func IsConflict(err error) bool { return statusIs(err, http.StatusConflict) }

// UnknownOutcomeError means a MUTATION failed at the transport layer, so
// whether the server acted is unknown. Not retryable: the work may be done,
// and retrying blind is how one fork becomes two VMs. Retry only with the same
// IdempotencyKey.
type UnknownOutcomeError struct {
	Method string
	Path   string
	Err    error
}

func (e *UnknownOutcomeError) Error() string {
	return fmt.Sprintf(
		"network failure during %s %s: outcome unknown. Retry with the same "+
			"IdempotencyKey, or reconcile state first", e.Method, e.Path)
}

func (e *UnknownOutcomeError) Unwrap() error { return e.Err }
