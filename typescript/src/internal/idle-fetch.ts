/** Client-side inactivity timeout; response progress resets it. */
export class RequestIdleTimeoutError extends Error {
  constructor(readonly timeoutMs: number, readonly responseStarted = false) { super(`Request idle timeout after ${timeoutMs}ms`); }
}

export async function idleFetch(
  fetchImpl: typeof fetch, input: RequestInfo | URL, init: RequestInit, timeoutMs: number,
): Promise<Response> {
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw new RangeError("timeout must be finite and nonnegative");
  if (!timeoutMs) return fetchImpl(input, init);
  const controller = new AbortController();
  const abort = () => controller.abort(init.signal?.reason);
  init.signal?.addEventListener("abort", abort, { once: true });
  if (init.signal?.aborted) abort();
  let responseStarted = false;
  let timer: ReturnType<typeof setTimeout>;
  const reset = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      init.signal?.removeEventListener("abort", abort);
      controller.abort(new RequestIdleTimeoutError(timeoutMs, responseStarted));
    }, timeoutMs);
    timer.unref?.();
  };
  const close = () => { clearTimeout(timer); init.signal?.removeEventListener("abort", abort); };
  reset();
  try {
    const response = await fetchImpl(input, { ...init, signal: controller.signal });
    responseStarted = true;
    if (!response.body) { close(); return response; }
    const reader = response.body.getReader();
    reset();
    const body = new ReadableStream<Uint8Array>({
      async pull(stream) {
        try {
          const { done, value } = await reader.read();
          if (done) { close(); reader.releaseLock(); stream.close(); }
          else { reset(); stream.enqueue(value); }
        } catch (error) {
          close();
          reader.releaseLock();
          stream.error(controller.signal.aborted ? controller.signal.reason : error);
        }
      },
      async cancel(reason) {
        close();
        controller.abort(reason);
        try { await reader.cancel(reason); } finally { reader.releaseLock(); }
      },
    }, { highWaterMark: 0 });
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  } catch (error) {
    close();
    throw controller.signal.aborted ? controller.signal.reason : error;
  }
}
