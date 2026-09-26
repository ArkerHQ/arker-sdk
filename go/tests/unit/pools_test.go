package unit

import (
	"context"
	"encoding/json"
	"errors"
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

func TestPoolReadsGoToTheControlPlane(t *testing.T) {
	var got []string
	c := twoPlane(t, reject(t, "regional"), func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.Method+" "+r.URL.RequestURI())
		switch {
		case r.URL.Path == "/v1/pools":
			fmt.Fprint(w, `{"pools":[`+poolBody+`],"purchases_enabled":true,"next_cursor":null}`)
		case strings.HasSuffix(r.URL.Path, "/usage"):
			fmt.Fprint(w, `{"pool_id":"`+poolID+`","resources_allocated":{"vcpu":2},"allocation_observed_at":null}`)
		default:
			fmt.Fprint(w, poolBody)
		}
	})
	ctx := context.Background()

	list, err := c.ListPools(ctx, arker.ListPoolsOptions{Limit: 5})
	if err != nil || len(list.Pools) != 1 || !list.PurchasesEnabled {
		t.Fatalf("list: %+v %v", list, err)
	}
	if _, err := c.GetPool(ctx, poolID); err != nil {
		t.Fatalf("get: %v", err)
	}
	usage, err := c.GetPoolUsage(ctx, poolID)
	if err != nil || usage.ResourcesAllocated.VCPU != 2 {
		t.Fatalf("usage: %+v %v", usage, err)
	}
	want := []string{"GET /v1/pools?limit=5", "GET /v1/pools/" + poolID, "GET /v1/pools/" + poolID + "/usage"}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("control plane saw %q, want %q", got, want)
	}
}

func TestRenamePoolSendsNullToClear(t *testing.T) {
	c, requests := poolServer(t, nil)

	if _, err := c.RenamePool(context.Background(), poolID, nil); err != nil {
		t.Fatalf("rename: %v", err)
	}
	got := (*requests)[0]
	if got.method != http.MethodPatch {
		t.Fatalf("method %s", got.method)
	}
	if name, present := got.body["name"]; !present || name != nil {
		t.Fatalf("body %v; a nil name must be sent as null", got.body)
	}
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

func TestCreatePoolSurfacesAPriceChange(t *testing.T) {
	c, requests := poolServer(t, func(_ int, w http.ResponseWriter) bool {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"error":{"code":"conflict","message":"The price changed.","details":{"resource":"pool_quote"}}}`)
		return true
	})

	_, err := c.CreatePool(context.Background(), poolTerms)
	var apiErr *arker.Error
	if !errors.As(err, &apiErr) || apiErr.Code != "conflict" {
		t.Fatalf("want a conflict, got %v", err)
	}
	if len(*requests) != 2 {
		t.Fatalf("a price change must not be retried or re-bought: %d requests", len(*requests))
	}
}

func TestCreatePoolNeedsAPlacement(t *testing.T) {
	c := twoPlane(t, reject(t, "regional"), reject(t, "control"))
	// twoPlane sets BaseURL, which leaves Provider and Region empty.
	if _, err := c.CreatePool(context.Background(), poolTerms); err == nil {
		t.Fatal("a pool without a placement reported success")
	}
}
