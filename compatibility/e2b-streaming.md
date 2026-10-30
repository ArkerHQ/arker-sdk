# Command retention and streamed uploads

With E2B 2.62, the Arker adapter exposes the native command and filesystem
components. It does not accumulate a second copy of command output or buffer
an iterable upload in an adapter wrapper.

`retain_output=False` in Python and `retainOutput: false` in TypeScript keep
stdout/stderr callbacks active while the result's accumulated output is `None`
or `undefined`, respectively. This applies to foreground commands, background
handles, and command reconnection. In synchronous Python, callbacks for a
background or connected handle are passed to `handle.wait()`; asynchronous
Python and TypeScript accept them when creating or connecting the handle.

Python synchronous uploads accept byte iterables; asynchronous uploads accept
async byte iterables. TypeScript accepts async iterables of `Uint8Array` and
`ReadableStream` inputs. These use the native octet-stream upload path, which
requires envd 0.5.7 or later. Explicit multipart selection for an iterable is
rejected; Python also rejects an iterable intended for the other execution
mode. The adapter preserves native exception types and endpoint-scoped
authentication. The TypeScript transport explicitly cancels its upload reader
on abort, including when the fetch implementation only rejects the request.
It also finalizes unfinished uploads when a response completes or fails,
without aborting a returned response merely to stop its upload.

Local HTTP regressions cover retained and unretained output with callbacks,
upload dispatch before complete producer consumption, exact uploaded bytes,
pre-dispatch validation, and asynchronous cancellation of both the producer
and its request. The TypeScript generator test releases a pending yield after
abort; it does not promise to interrupt arbitrary application code blocked
inside a producer. They exercise the real native components through the scoped
transport; they do not establish live guest or hosted-provider parity.
