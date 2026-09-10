// Fork a VM, run commands in it, and clean up.
//
//	ARKER_API_KEY=... ARKER_BASE_URL=https://<host>/api \
//	  go run ./examples/fork-run [source_vm_name]
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

func main() {
	client, err := arker.New(arker.Options{}) // reads ARKER_API_KEY + ARKER_BASE_URL
	if err != nil {
		log.Fatal(err)
	}
	source := "ubuntu-base"
	if len(os.Args) > 1 {
		source = os.Args[1]
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: source})
	if err != nil {
		log.Fatalf("fork: %v", err)
	}
	fmt.Printf("forked %s from %s\n", vm.ID, source)
	// Always delete: an abandoned VM keeps billing.
	defer func() {
		clean, done := context.WithTimeout(context.Background(), 3*time.Minute)
		defer done()
		if err := vm.Delete(clean); err != nil {
			log.Printf("delete %s: %v", vm.ID, err)
		}
	}()

	out, err := vm.Run(ctx, arker.RunRequest{Command: "uname -a && nproc"})
	if err != nil {
		log.Fatalf("run: %v", err)
	}
	fmt.Print(out.Stdout)

	// Sessions are tabs: each keeps its own cwd, environment and history, and
	// each handles one run at a time.
	session, err := vm.CreateSession(ctx, arker.CreateSessionRequest{
		CWD: "/tmp", Env: map[string]string{"GREETING": "hello"},
	})
	if err != nil {
		log.Fatalf("create session: %v", err)
	}
	out, err = vm.Run(ctx, arker.RunRequest{Command: `echo "$GREETING from $(pwd)"`, SessionID: session.SessionID})
	if err != nil {
		log.Fatalf("run in session: %v", err)
	}
	fmt.Print(out.Stdout)

	// A long task belongs in a session of its own, backgrounded, so later work
	// does not interrupt it.
	worker, err := vm.CreateSession(ctx, arker.CreateSessionRequest{})
	if err != nil {
		log.Fatalf("create worker session: %v", err)
	}
	started, err := vm.Run(ctx, arker.RunRequest{
		Command: "sleep 30 && echo finished", SessionID: worker.SessionID, TimeToBackground: arker.Ptr(0),
	})
	if err != nil {
		log.Fatalf("background run: %v", err)
	}
	fmt.Printf("backgrounded run %s; polling\n", started.RunID)

	for {
		record, err := vm.GetRun(ctx, started.RunID)
		if err != nil {
			log.Fatalf("poll: %v", err)
		}
		if record.State != "running" && record.State != "pending" {
			fmt.Printf("run %s finished as %q: %s", record.RunID, record.State, record.Stdout)
			return
		}
		time.Sleep(2 * time.Second)
	}
}
