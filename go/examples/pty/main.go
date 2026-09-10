// Interactive PTY — drop your local terminal into a live shell inside a VM.
//
//	ARKER_API_KEY=... ARKER_BASE_URL=https://<host>/api \
//	  go run ./examples/pty [vm_id]
//
// With no argument it forks ARKER_SOURCE_VM. Exit by leaving the shell
// (`exit` / Ctrl-D).
package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"time"

	arker "github.com/ArkerHQ/arker-sdk/go"
	"golang.org/x/term"
)

func main() {
	client, err := arker.New(arker.Options{})
	if err != nil {
		log.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	var vm *arker.VM
	if len(os.Args) > 1 {
		vm = client.VM(os.Args[1])
		if err := vm.Refresh(ctx); err != nil {
			log.Fatalf("refresh: %v", err)
		}
	} else {
		source := os.Getenv("ARKER_SOURCE_VM")
		if source == "" {
			source = "ubuntu-base"
		}
		if vm, err = client.Fork(ctx, arker.ForkRequest{SourceVMName: source}); err != nil {
			log.Fatalf("fork: %v", err)
		}
		fmt.Fprintf(os.Stderr, "forked %s\n", vm.ID)
	}

	fd := int(os.Stdin.Fd())
	cols, rows := 80, 24
	if term.IsTerminal(fd) {
		if w, h, err := term.GetSize(fd); err == nil {
			cols, rows = w, h
		}
	}

	// Plain=false: a human is watching, so keep colour and cursor addressing.
	pty, err := vm.ConnectPTY(ctx, arker.PTYOptions{
		Cols: &cols, Rows: &rows, Plain: arker.Ptr(false),
		OnData: func(b []byte) { _, _ = os.Stdout.Write(b) },
	})
	if err != nil {
		log.Fatalf("connect pty: %v", err)
	}

	// Raw mode forwards every keystroke verbatim, Ctrl-C included.
	if term.IsTerminal(fd) {
		state, err := term.MakeRaw(fd)
		if err != nil {
			log.Fatalf("raw mode: %v", err)
		}
		defer func() { _ = term.Restore(fd, state) }()
	}
	fmt.Fprintln(os.Stderr, "[connected] you're in the VM.")

	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := os.Stdin.Read(buf)
			if n > 0 {
				if err := pty.Send(buf[:n]); err != nil {
					return
				}
			}
			if err != nil {
				if err != io.EOF {
					log.Printf("stdin: %v", err)
				}
				_ = pty.Kill()
				return
			}
		}
	}()

	<-pty.Done()
	if err := pty.Err(); err != nil {
		log.Printf("pty ended: %v", err)
	}
}
