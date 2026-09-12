// Package arker is the Go client for the Arker VM API. Its surface mirrors the
// Python and TypeScript SDKs: a Client for org-wide calls, and a VM handle for
// everything scoped to one machine.
//
//	client, _ := arker.New(arker.Options{APIKey: os.Getenv("ARKER_API_KEY")})
//	vm, _ := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base"})
//	res, _ := vm.Run(ctx, arker.RunRequest{Command: "echo hi"})
package arker

import (
	"crypto/rand"
	"encoding/hex"
	"net/url"
	"time"
)

// Version is this SDK's release, sent as part of the User-Agent.
const Version = "0.2.0"

const (
	// DefaultControlBaseURL serves the org-wide, placement-independent routes:
	// /v1/vms, /v1/runs, /v1/regions, /v1/whoami. Everything else needs a
	// regional base URL, which is why placement is configured separately.
	DefaultControlBaseURL = "https://arker.ai/api"

	// ArkerOrgID selects an Arker-owned public source in a ForkRequest.
	ArkerOrgID = "ArkerHQ"

	// Must exceed the server's sync window or the request is abandoned exactly
	// as the background ack arrives, and Run never gets to poll.
	defaultTimeout    = 300 * time.Second
	streamTimeout     = 600 * time.Second
	defaultAttempts   = 4 // TOTAL wire attempts, not four on top of the first
	defaultBaseDelay  = 200 * time.Millisecond
	defaultMaxDelay   = 2 * time.Second
	defaultJitter     = 50 * time.Millisecond
	streamMaxBytes    = 64 << 20 // the router's proxy body cap; 413 above it
	compressSampleMin = 256 << 10
	compressRatio     = 0.9
	hashConcurrency   = 8
)

// Terminal run states. Anything absent is treated as non-terminal, so an
// unknown future state degrades to "keep polling" rather than a false
// completion. "pending" is a run queued behind an earlier one on its session.
var terminalRunStates = map[string]bool{"completed": true, "failed": true, "cancelled": true}

// Codes the service marks retryable by name when it omits the `retryable` flag.
var retryableCodes = map[string]bool{
	"unavailable": true, "bad_gateway": true, "stale_route": true, "capacity_unavailable": true,
}

// 429 and 503 are refusals -- the origin did no work. 502 and 504 are gateway
// errors where it may already have acted, and are only safe to retry because
// Fork carries an Idempotency-Key.
var retryableStatus = map[int]bool{429: true, 502: true, 503: true, 504: true}

// PlainPTYEnv makes a PTY emit plain text. The usual consumer of a PTY here is
// a program, and stripping escape sequences after the fact is lossy: "\x1b[2K"
// plus "\r" means "rewrite this line", so dropping the codes concatenates every
// frame of a progress bar. Far better to ask the program not to emit them.
// TERM=dumb also disables cursor addressing, so a full-screen TUI will degrade.
var PlainPTYEnv = map[string]string{
	"TERM": "dumb", "NO_COLOR": "1", "FORCE_COLOR": "0", "CLICOLOR": "0",
}

// Ptr returns a pointer to v, for the optional fields on request structs.
func Ptr[T any](v T) *T { return &v }

func segment(v string) string { return url.PathEscape(v) }

func vmPath(vmID, suffix string) string { return "/v1/vms/" + segment(vmID) + suffix }

func computeBaseURL(provider, region string) string {
	return "https://" + provider + "-" + region + ".arker.ai/api"
}

// newIdempotencyKey mints a key for one logical operation: inside the server's
// 64-char limit, same shape as the Python and TypeScript SDKs.
func newIdempotencyKey(prefix string) string {
	buf := make([]byte, 16)
	mustRandom(buf)
	return prefix + hex.EncodeToString(buf)
}

func mustRandom(b []byte) {
	if _, err := rand.Read(b); err != nil {
		// A predictable key would replay the wrong VM across callers, which is
		// worse than failing the call.
		panic("arker: crypto/rand unavailable: " + err.Error())
	}
}
