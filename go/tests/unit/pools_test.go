package unit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ArkerHQ/arker-sdk/go"
)

const poolID = "7d1f7c2e-8a53-4c1e-9f3a-2b6d0c4e5f61"

const poolBody = `{"pool_id":"` + poolID + `","name":null,"provider":"aws","region":"us-west-2",` +
	`"resources":{"vcpu":8,"memory_mib":32768},"duration_seconds":2592000,"status":"active",` +
	`"created_at":"2026-09-25T00:00:00Z","starts_at":"2026-09-25T00:00:00Z","ends_at":"2026-10-25T00:00:00Z",` +
	`"currency":"usd","amount_cents":12345,"invoice_id":null}`

const poolQuoteBody = `{"name":null,"catalog_version":"v1","baseline_cents":15000,"discount_percent":17.7,` +
	`"provider":"aws","region":"us-west-2","resources":{"vcpu":8,"memory_mib":32768},` +
	`"duration_seconds":2592000,"currency":"usd","amount_cents":12345}`

var poolTerms = arker.CreatePoolRequest{
	Resources:       arker.PoolResources{VCPU: 8, MemoryMiB: 32768},
	DurationSeconds: 2592000,
}

type poolRequest struct {
	method, path, key string
	body              map[string]any
}

// poolServer answers the pool routes on a control-plane server with a
// placement-configured client, recording every request.
func poolServer(t *testing.T, buy func(n int, w http.ResponseWriter) bool) (*arker.Client, *[]poolRequest) {
	t.Helper()
	var requests []poolRequest
	buys := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		if len(raw) > 0 {
			_ = json.Unmarshal(raw, &body)
		}
		requests = append(requests, poolRequest{r.Method, r.URL.Path, r.Header.Get("Idempotency-Key"), body})
		switch {
		case r.URL.Path == "/v1/pools/quote":
			fmt.Fprint(w, poolQuoteBody)
		case r.Method == http.MethodPost && r.URL.Path == "/v1/pools":
			buys++
			if buy != nil && buy(buys, w) {
				return
			}
			fmt.Fprint(w, poolBody)
		default:
			fmt.Fprint(w, poolBody)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := arker.New(arker.Options{
		APIKey: "k", Provider: "aws", Region: "us-west-2", ControlBaseURL: srv.URL, Retry: fastRetry,
	})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	return c, &requests
}

func TestCreatePoolBuysAtTheQuotedPrice(t *testing.T) {
	c, requests := poolServer(t, nil)

	pool, err := c.CreatePool(context.Background(), poolTerms)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if pool.PoolID != poolID {
		t.Fatalf("pool %+v", pool)
	}
	if len(*requests) != 2 {
		t.Fatalf("requests %+v", *requests)
	}
	quote, buy := (*requests)[0], (*requests)[1]
	// Placement comes from the client when the caller does not give one.
	if quote.body["provider"] != "aws" || quote.body["region"] != "us-west-2" {
		t.Fatalf("quote body %v", quote.body)
	}
	if quote.key != "" {
		t.Fatalf("the quote sent Idempotency-Key %q", quote.key)
	}
	if buy.body["amount_cents"] != float64(12345) || !strings.HasPrefix(buy.key, "sdk-pool-") {
		t.Fatalf("buy %+v", buy)
	}
	if _, leaked := buy.body["IdempotencyKey"]; leaked {
		t.Fatal("the idempotency key leaked into the body")
	}
}

func TestCreatePoolRetryReplaysTheSamePurchase(t *testing.T) {
	// A purchase whose response was lost must be retried with the same key and
	// price, so the service returns the original pool instead of a second one.
	c, requests := poolServer(t, func(n int, w http.ResponseWriter) bool {
		if n > 1 {
			return false
		}
		conn, _, _ := w.(http.Hijacker).Hijack()
		_ = conn.Close()
		return true
	})

	req := poolTerms
	req.IdempotencyKey = "buy-1"
	if _, err := c.CreatePool(context.Background(), req); err != nil {
		t.Fatalf("create: %v", err)
	}
	var keys []string
	for _, r := range *requests {
		if r.path == "/v1/pools" {
			keys = append(keys, r.key)
		}
	}
	if strings.Join(keys, ",") != "buy-1,buy-1" {
		t.Fatalf("purchase keys %q", keys)
	}
}
