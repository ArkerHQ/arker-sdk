package arker

import (
	"errors"
	"fmt"
	"net/http"
)

// Error is an arkerd API failure. arkerd answers with a FLAT envelope --
// {"error":{"code","message","retryable","retry_after"}} -- with no nested
// detail object, and `retryable` is frequently ABSENT rather than false.
// Callers should branch on Code, never on substrings of Message.
type Error struct {
	Code       string
	Message    string
	StatusCode int
	// Retryable is what the SERVER said. Absent means "unspecified", not
	// "no" -- see Retry.shouldRetry for how that is resolved.
	Retryable  *bool
	RetryAfter *float64
}

func (e *Error) Error() string {
	return fmt.Sprintf("arker api status %d: %s: %s", e.StatusCode, e.Code, e.Message)
}

// IsNotFound reports a 404. Note this is ORG-SCOPED: a resource in another
// org is indistinguishable from one that never existed, by design.
func IsNotFound(err error) bool {
	var apiErr *Error
	return errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusNotFound
}

// IsConflict reports a 409 -- for a fork, an Idempotency-Key reused for a
// DIFFERENT request.
func IsConflict(err error) bool {
	var apiErr *Error
	return errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusConflict
}

// UnknownOutcomeError is returned when a MUTATION failed at the transport
// layer, so whether the server acted is genuinely unknown.
//
// This is not a retryable error and must not be treated as one. The server
// may have completed the work; retrying blind is how a fork becomes two VMs,
// one of them orphaned and billable. Retry only with the SAME
// IdempotencyKey, which lets the server recognise the replay.
type UnknownOutcomeError struct {
	Method string
	Path   string
	Err    error
}

func (e *UnknownOutcomeError) Error() string {
	return fmt.Sprintf(
		"network failure during %s %s: the operation outcome is unknown. "+
			"Retry with the same IdempotencyKey, or reconcile state before retrying",
		e.Method, e.Path,
	)
}

func (e *UnknownOutcomeError) Unwrap() error { return e.Err }
