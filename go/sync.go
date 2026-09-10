package arker

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// ReadFile reads a file out of this VM.
//
// Python and TypeScript expose read and write as one `sync(path, data?)`; Go
// has no optional argument, and splitting them also removes the nil-versus-
// empty ambiguity when writing a zero-length file.
func (v *VM) ReadFile(ctx context.Context, path string) ([]byte, error) {
	var out struct {
		Content      string `json:"content"`
		Encoding     string `json:"encoding"`
		PresignedURL string `json:"presigned_url"`
	}
	if _, err := v.do(ctx, http.MethodPost, v.path("/sync"),
		map[string]string{"op": "read", "path": path}, &out); err != nil {
		return nil, err
	}
	if out.PresignedURL == "" {
		if out.Encoding == "base64" {
			return base64.StdEncoding.DecodeString(out.Content)
		}
		return []byte(out.Content), nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, out.PresignedURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := v.client.http.Do(req)
	if err != nil {
		return nil, err
	}
	status, raw, err := drain(resp)
	if err != nil {
		return nil, err
	}
	if status >= http.StatusBadRequest {
		return nil, decodeError(status, raw)
	}
	return raw, nil
}

// WriteFile writes data to path in this VM, creating parent directories.
//
// The bytes stream straight to the guest's disk at every size: the destination
// is the guest filesystem, not object storage, so there is nothing to gain by
// detouring through it.
func (v *VM) WriteFile(ctx context.Context, path string, data []byte) error {
	return v.streamPost(ctx, map[string]string{"path": path, "size": strconv.Itoa(len(data))},
		func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(data)), nil }, "sync write")
}

// streamPost is the single /sync-stream call site. body is a factory, not a
// value, so a retried attempt gets fresh bytes -- a consumed stream cannot be
// replayed.
func (v *VM) streamPost(ctx context.Context, params map[string]string, body func() (io.ReadCloser, error), what string) error {
	q := newQuery()
	for k, val := range params {
		q.str(k, val)
	}
	url := v.baseURL + q.on(v.path("/sync-stream"))
	client := &http.Client{Timeout: streamTimeout, Transport: v.client.http.Transport}

	for attempt := range v.client.retry.Attempts {
		reader, err := body()
		if err != nil {
			return err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, reader)
		if err != nil {
			return err
		}
		v.client.auth(req.Header)
		req.Header.Set("Content-Type", "application/octet-stream")

		resp, err := client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return &UnknownOutcomeError{http.MethodPost, "/sync-stream", err}
		}
		status, raw, readErr := drain(resp)
		if status < http.StatusBadRequest && readErr == nil {
			return nil
		}
		apiErr := decodeError(status, raw)
		if apiErr.Message == "" {
			apiErr.Message = fmt.Sprintf("%s failed (%d)", what, status)
		}
		// 413 is the router's body cap, not a transient fault.
		if !retryable(apiErr) || attempt == v.client.retry.Attempts-1 || !v.client.backoff(ctx, attempt, apiErr) {
			return apiErr
		}
	}
	return fmt.Errorf("arker: %s exhausted retries", what)
}

// SyncDirResult reports what a SyncDir call moved.
type SyncDirResult struct {
	Sent      int
	Skipped   int
	BytesSent int64
	// ManifestTruncated means the server hit its walk cap. Past the cap every
	// omitted file looks absent, so the diff re-uploads it: correct, but the
	// delta sync silently degrades to a full one.
	ManifestTruncated bool
}

// SyncDirOptions tunes a directory sync.
type SyncDirOptions struct {
	// Ignore is called with each file's context-relative, slash-separated path;
	// returning true drops it. Applied BEFORE hashing, so an ignored file costs
	// nothing and cannot perturb the diff.
	Ignore func(relPath string) bool
	// Cache, if non-nil, skips re-hashing local files whose size and mtime are
	// unchanged. It never decides remote state, so it cannot cause a stale
	// upload -- worst case it hashes a file it did not need to.
	Cache *SyncCache
}

// SyncCache memoises local file hashes across SyncDir calls. The zero value is
// not usable; call NewSyncCache.
type SyncCache struct {
	mu      sync.Mutex
	entries map[string]cacheEntry
}

type cacheEntry struct {
	size  int64
	mtime int64
	hash  string
}

// NewSyncCache returns a cache to reuse across SyncDir calls.
func NewSyncCache() *SyncCache { return &SyncCache{entries: map[string]cacheEntry{}} }

func (c *SyncCache) get(path string, size, mtime int64) (string, bool) {
	if c == nil {
		return "", false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[path]
	return e.hash, ok && e.size == size && e.mtime == mtime
}

func (c *SyncCache) put(path string, size, mtime int64, hash string) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.entries[path] = cacheEntry{size, mtime, hash}
}

type localFile struct {
	rel, abs string
	size     int64
	mtime    int64
	hash     string
}

// SyncDir recursively syncs localDir INTO this VM at remoteDir, rsync-style:
// fetch the VM's per-file sha256 manifest in ONE request (host-first, so it
// works on a never-run VM), diff it against the local tree, and upload only the
// new or changed files packed into a single tarball the guest extracts.
//
// The remote manifest is authoritative, so a failure fails safe: any file the
// manifest omits is simply re-sent next call.
func (v *VM) SyncDir(ctx context.Context, localDir, remoteDir string, opts SyncDirOptions) (*SyncDirResult, error) {
	localRoot, err := filepath.Abs(localDir)
	if err != nil {
		return nil, err
	}
	remoteRoot := "/" + strings.Trim(remoteDir, "/")

	remote, truncated, err := v.remoteManifest(ctx, remoteRoot)
	if err != nil {
		return nil, err
	}

	// Regular files only -- the manifest lists regular files, so a symlink
	// would always look missing and be re-sent forever.
	var files []*localFile
	if err := filepath.Walk(localRoot, func(abs string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || !info.Mode().IsRegular() {
			return err
		}
		rel, err := filepath.Rel(localRoot, abs)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if opts.Ignore != nil && opts.Ignore(rel) {
			return nil
		}
		files = append(files, &localFile{rel: rel, abs: abs, size: info.Size(), mtime: info.ModTime().UnixNano()})
		return nil
	}); err != nil {
		return nil, err
	}
	sort.Slice(files, func(i, j int) bool { return files[i].rel < files[j].rel })

	if err := hashAll(files, opts.Cache); err != nil {
		return nil, err
	}

	// Diff in sorted order so the tarball is reproducible and the counters are
	// deterministic regardless of which hash finished first.
	result := &SyncDirResult{ManifestTruncated: truncated}
	var changed []*localFile
	for _, f := range files {
		if remote[f.rel] == f.hash {
			result.Skipped++
			continue
		}
		changed = append(changed, f)
		result.Sent++
		result.BytesSent += f.size
	}
	if len(changed) == 0 {
		return result, nil
	}
	return result, v.uploadTarball(ctx, changed, remoteRoot)
}

func hashAll(files []*localFile, cache *SyncCache) error {
	sem := make(chan struct{}, hashConcurrency)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var firstErr error
	for _, f := range files {
		if hash, ok := cache.get(f.abs, f.size, f.mtime); ok {
			f.hash = hash
			continue
		}
		wg.Add(1)
		go func(f *localFile) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			hash, err := hashFile(f.abs)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				if firstErr == nil {
					firstErr = err
				}
				return
			}
			f.hash = hash
			cache.put(f.abs, f.size, f.mtime, hash)
		}(f)
	}
	wg.Wait()
	return firstErr
}

func hashFile(path string) (string, error) {
	fh, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer func() { _ = fh.Close() }()
	sum := sha256.New()
	if _, err := io.Copy(sum, fh); err != nil {
		return "", err
	}
	return hex.EncodeToString(sum.Sum(nil)), nil
}

// remoteManifest fetches {relPath: sha256} under path. A path that does not
// exist yields an empty manifest, so everything is sent.
func (v *VM) remoteManifest(ctx context.Context, path string) (map[string]string, bool, error) {
	var out struct {
		Entries []struct {
			Path string `json:"path"`
			Hash string `json:"hash"`
		} `json:"entries"`
		Truncated bool `json:"truncated"`
	}
	if _, err := v.do(ctx, http.MethodPost, v.path("/sync"),
		map[string]string{"op": "manifest", "path": path}, &out); err != nil {
		return nil, false, err
	}
	manifest := make(map[string]string, len(out.Entries))
	for _, e := range out.Entries {
		manifest[e.Path] = e.Hash
	}
	return manifest, out.Truncated, nil
}

// uploadTarball packs the changed files into ONE tar and has the GUEST extract
// it, so the writes are always consistent with its own filesystem -- and one
// stream plus one extract beats one write per file.
func (v *VM) uploadTarball(ctx context.Context, changed []*localFile, remoteRoot string) error {
	compress := worthCompressing(changed)
	mode := "tar"
	if compress {
		mode = "tar.gz"
	}
	tmp, err := os.CreateTemp("", "arker-sync-*."+mode)
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	if err := writeTar(tmp, changed, compress); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	info, err := os.Stat(tmp.Name())
	if err != nil {
		return err
	}
	if info.Size() > streamMaxBytes {
		return &Error{Code: "payload_too_large", StatusCode: 413, Message: fmt.Sprintf(
			"arker: sync_dir tarball is %d bytes, above the %d-byte edge limit; sync fewer files per call",
			info.Size(), streamMaxBytes)}
	}
	// size comes from stat, not a buffered length: the router reads it to
	// decide whether to forward the body streamed, so it must be exact.
	return v.streamPost(ctx,
		map[string]string{"path": remoteRoot, "size": strconv.FormatInt(info.Size(), 10), "extract": mode},
		func() (io.ReadCloser, error) { return os.Open(tmp.Name()) },
		"sync-stream extract")
}

func writeTar(out io.Writer, changed []*localFile, compress bool) error {
	var gz *gzip.Writer
	if compress {
		gz = gzip.NewWriter(out)
		out = gz
	}
	tw := tar.NewWriter(out)
	for _, f := range changed {
		info, err := os.Stat(f.abs)
		if err != nil {
			return err
		}
		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		header.Name = f.rel
		if err := tw.WriteHeader(header); err != nil {
			return err
		}
		fh, err := os.Open(f.abs)
		if err != nil {
			return err
		}
		_, err = io.CopyN(tw, fh, info.Size())
		_ = fh.Close()
		if err != nil {
			return err
		}
	}
	if err := tw.Close(); err != nil {
		return err
	}
	if gz != nil {
		return gz.Close()
	}
	return nil
}

// worthCompressing samples the head of a handful of files rather than
// compressing everything twice. Source trees compress ~4:1, but the guest pays
// ~3.4x a plain untar to gunzip, so compressing an already-compressed tree
// loses at both ends.
func worthCompressing(changed []*localFile) bool {
	var raw, packed int64
	for _, f := range changed[:min(8, len(changed))] {
		fh, err := os.Open(f.abs)
		if err != nil {
			continue // unreadable sample; the tar step will surface it
		}
		chunk := make([]byte, 128<<10)
		n, _ := io.ReadFull(fh, chunk)
		_ = fh.Close()
		if n <= 0 {
			continue
		}
		var buf bytes.Buffer
		gz := gzip.NewWriter(&buf)
		_, _ = gz.Write(chunk[:n])
		_ = gz.Close()
		raw += int64(n)
		packed += int64(buf.Len())
	}
	if raw < compressSampleMin {
		return true
	}
	return float64(packed)/float64(raw) < compressRatio
}
