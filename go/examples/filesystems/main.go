// Share one filesystem between two VMs.
//
//	ARKER_API_KEY=... ARKER_BASE_URL=https://<host>/api \
//	  go run ./examples/filesystems
package main

import (
	"context"
	"fmt"
	"log"
	"time"

	arker "github.com/ArkerHQ/arker-sdk/go"
)

func main() {
	client, err := arker.New(arker.Options{})
	if err != nil {
		log.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()

	who, err := client.Whoami(ctx)
	if err != nil {
		log.Fatalf("whoami: %v", err)
	}
	fmt.Printf("org %s (%s)\n", who.OrgID, who.OrgName)

	fs, err := client.CreateFilesystem(ctx, fmt.Sprintf("shared-%d", time.Now().Unix()))
	if err != nil {
		log.Fatalf("create filesystem: %v", err)
	}
	fmt.Printf("filesystem %s\n", fs.FilesystemID)
	defer func() {
		clean, done := context.WithTimeout(context.Background(), 3*time.Minute)
		defer done()
		_ = client.DeleteFilesystem(clean, fs.FilesystemID)
	}()

	var vms []*arker.VM
	defer func() {
		for _, vm := range vms {
			clean, done := context.WithTimeout(context.Background(), 3*time.Minute)
			_ = vm.Delete(clean)
			done()
		}
	}()

	for _, role := range []string{"writer", "reader"} {
		vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base", Name: role})
		if err != nil {
			log.Fatalf("fork %s: %v", role, err)
		}
		vms = append(vms, vm)
		if _, err := vm.CreateSync(ctx, fs.FilesystemID, "/mnt/shared"); err != nil {
			log.Fatalf("bind filesystem into %s: %v", role, err)
		}
		fmt.Printf("%s = %s, filesystem mounted at /mnt/shared\n", role, vm.ID)
	}

	writer, reader := vms[0], vms[1]
	if _, err := writer.Run(ctx, arker.RunRequest{
		Command: "echo 'written by the first VM' > /mnt/shared/note.txt && sync",
	}); err != nil {
		log.Fatalf("write: %v", err)
	}
	out, err := reader.Run(ctx, arker.RunRequest{Command: "cat /mnt/shared/note.txt"})
	if err != nil {
		log.Fatalf("read: %v", err)
	}
	fmt.Printf("the second VM sees: %s", out.Stdout)

	syncs, err := reader.ListSyncs(ctx, arker.ListSyncsOptions{})
	if err != nil {
		log.Fatalf("list syncs: %v", err)
	}
	for _, s := range syncs.Syncs {
		fmt.Printf("sync %s -> %s (status %s)\n", s.SyncID, s.Path, s.Status)
	}
}
