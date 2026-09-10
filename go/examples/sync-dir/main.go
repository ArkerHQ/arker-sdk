// Push a local directory into a VM and run it there.
//
//	ARKER_API_KEY=... ARKER_BASE_URL=https://<host>/api \
//	  go run ./examples/sync-dir ./my-project
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

func main() {
	if len(os.Args) < 2 {
		log.Fatal("usage: sync-dir <local-directory>")
	}
	local := os.Args[1]

	client, err := arker.New(arker.Options{})
	if err != nil {
		log.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()

	vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base"})
	if err != nil {
		log.Fatalf("fork: %v", err)
	}
	defer func() {
		clean, done := context.WithTimeout(context.Background(), 3*time.Minute)
		defer done()
		_ = vm.Delete(clean)
	}()
	fmt.Printf("forked %s\n", vm.ID)

	// The cache is a pure accelerator across calls: it skips re-hashing files
	// whose size and mtime are unchanged, and never decides remote state.
	cache := arker.NewSyncCache()
	ignore := func(rel string) bool {
		for _, skip := range []string{".git/", "node_modules/", "target/", ".venv/"} {
			if strings.HasPrefix(rel, skip) || strings.Contains(rel, "/"+skip) {
				return true
			}
		}
		return false
	}

	result, err := vm.SyncDir(ctx, local, "/workspace", arker.SyncDirOptions{Ignore: ignore, Cache: cache})
	if err != nil {
		log.Fatalf("sync_dir: %v", err)
	}
	fmt.Printf("sent %d files (%d bytes), skipped %d\n", result.Sent, result.BytesSent, result.Skipped)
	if result.ManifestTruncated {
		fmt.Println("warning: the server capped its walk, so this degraded to a full sync")
	}

	out, err := vm.Run(ctx, arker.RunRequest{Command: "ls -la /workspace | head -20"})
	if err != nil {
		log.Fatalf("run: %v", err)
	}
	fmt.Print(out.Stdout)

	// A second call after no local edits sends nothing: the remote manifest is
	// authoritative and everything already matches.
	again, err := vm.SyncDir(ctx, local, "/workspace", arker.SyncDirOptions{Ignore: ignore, Cache: cache})
	if err != nil {
		log.Fatalf("second sync_dir: %v", err)
	}
	fmt.Printf("re-sync sent %d, skipped %d\n", again.Sent, again.Skipped)
}
