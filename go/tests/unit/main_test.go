package unit

import (
	"os"
	"testing"
)

// Client construction falls back to ARKER_* for the key, the base URL and the
// placement, so a developer with those exported -- which is exactly what
// running the live suite requires -- got failures from their own shell rather
// than from the code: with ARKER_BASE_URL set, a client built with a provider
// and no region is accepted instead of rejected, and a derived regional URL
// comes back as whatever the environment said.
//
// CI passed only because a fresh runner happens to have none of them set. Unit
// tests must not depend on that, so the package clears them before any test
// builds a client.
func TestMain(m *testing.M) {
	for _, name := range []string{
		"ARKER_API_KEY",
		"ARKER_BASE_URL",
		"ARKER_CONTROL_BASE_URL",
		"ARKER_PROVIDER",
		"ARKER_REGION",
	} {
		_ = os.Unsetenv(name)
	}
	os.Exit(m.Run())
}
