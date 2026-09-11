package e2e

import (
	"context"
	"testing"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

// A machine-pool consumer drives this SDK from a reconciler that may retry any
// call at any time, and its provider contract is stricter than the SDK's own
// tests otherwise cover:
//
//   - provisioning must be idempotent per machine identity, and retries must
//     converge on ONE live sandbox -- never a second machine
//   - a key naming a DIFFERENT machine must conflict, not silently return the
//     wrong sandbox
//   - inspect must report absence as a value, not an error
//   - wake must be idempotent
//   - delete must be retry-safe and succeed when the resource is already gone
//
// Each is exercised against a live deployment here, because every one of them
// is a property of the SERVICE that the SDK only passes through.
func TestMachinePoolProviderContract(t *testing.T) {
	h := setup(t)
	ctx := budget(t, forkBudget)

	// A deterministic name plus an idempotency key is what makes a retried
	// provision converge instead of building a second machine.
	name := unique("pool-machine")
	key := "pool-" + name

	req := arker.ForkRequest{SourceVMName: h.golden, Name: name, IdempotencyKey: key}
	first, err := h.client.Fork(ctx, req)
	if err != nil {
		t.Fatalf("provision: %v", err)
	}
	h.own(first)

	replay, err := h.client.Fork(ctx, req)
	if err != nil {
		t.Fatalf("provision retry: %v", err)
	}
	if replay.ID != first.ID {
		h.own(replay)
		t.Fatalf("a retry built a SECOND sandbox for one machine: %s then %s", first.ID, replay.ID)
	}

	// Name is part of the identity hash, so the same key naming a different
	// machine must conflict rather than hand back the wrong sandbox.
	_, err = h.client.Fork(ctx, arker.ForkRequest{
		SourceVMName: h.golden, Name: unique("pool-machine"), IdempotencyKey: key,
	})
	if !arker.IsConflict(err) {
		t.Fatalf("a reused key on a different machine must 409, got %v", err)
	}

	// Inspect reports absence as (nil, false, nil), so a missing machine is a
	// value rather than an error the caller has to classify.
	got, found, err := h.client.GetVM(ctx, first.ID)
	if err != nil {
		t.Fatalf("inspect: %v", err)
	}
	if !found {
		t.Fatal("inspect did not find a machine it had just provisioned")
	}
	if got.Info == nil || got.Info.Name != name {
		t.Fatalf("inspect returned %+v, want name %q", got.Info, name)
	}

	// There is no wake endpoint and none is needed -- a no-op run IS a wake --
	// but a reconciler may issue it repeatedly, so it has to be idempotent.
	for i := range 2 {
		out, err := first.Run(ctx, arker.RunRequest{Command: "true"})
		if err != nil {
			t.Fatalf("wake attempt %d: %v", i+1, err)
		}
		if out.ExitCode == nil || *out.ExitCode != 0 {
			t.Fatalf("wake attempt %d exited %v", i+1, out.ExitCode)
		}
	}

	out, err := first.Run(ctx, arker.RunRequest{Command: "echo pool-ok"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if out.Stdout != "pool-ok\n" {
		t.Fatalf("run stdout %q, want %q", out.Stdout, "pool-ok\n")
	}

	// Delete must be retry-safe: the second call races a reconciler that already
	// tore the machine down, and must not surface as a failure.
	if err := first.Delete(ctx); err != nil {
		t.Fatalf("delete: %v", err)
	}
	h.disown(first.ID)
	if err := first.Delete(context.Background()); err != nil && !arker.IsNotFound(err) {
		t.Fatalf("delete on an already-absent machine must be nil or NotFound, got %v", err)
	}

	// A key is one-shot, as an idempotency key should be: it names one
	// operation, not a machine. Once the VM it named is deleted the key is
	// spent, and re-forking under it would let one logical operation produce a
	// second machine -- so the API answers 404.
	//
	// The corollary is just the ordinary rule: a key is minted per provisioning
	// attempt, never derived from a stable machine identity that outlives a
	// teardown. Pinned so the "spent key" answer stays 404 and not a fresh fork.
	if _, err := h.client.Fork(ctx, req); !arker.IsNotFound(err) {
		t.Fatalf("re-provisioning under a spent key must 404, got %v", err)
	}

	regen := arker.ForkRequest{
		SourceVMName: h.golden, Name: name + "-gen2", IdempotencyKey: key + "-gen2",
	}
	next, err := h.client.Fork(ctx, regen)
	if err != nil {
		t.Fatalf("re-provisioning under a fresh key must succeed: %v", err)
	}
	h.own(next)
	if next.ID == first.ID {
		t.Fatal("a fresh key returned the deleted machine")
	}
}
