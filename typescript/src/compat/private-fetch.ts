import { request as httpRequest } from "node:http";
import { Readable, type Duplex } from "node:stream";

interface Connector {
  connectPort(port: number): Promise<Duplex>;
}
// Blaxel's H2 path returns constructed Responses; its fallback uses Fetch.
type ResponseMode = "fetch" | "blaxel-h2";

function normalizeHeaders(
  init: RequestInit | undefined,
  mode: ResponseMode,
): RequestInit | undefined {
  if (!init?.headers) return init;
  const headers: Record<string, string> = {};
  const add = (name: string, value: string) => {
    headers[mode === "blaxel-h2" ? name.toLowerCase() : name] = value;
  };
  if (init.headers instanceof Headers)
    init.headers.forEach((value, name) => add(name, value));
  else
    for (const [name, value] of Array.isArray(init.headers)
      ? init.headers
      : Object.entries(init.headers))
      add(name, value);
  return { ...init, headers };
}

function immutable(headers: Headers): Headers {
  return new Proxy(headers, {
    get(target, property) {
      if (["set", "append", "delete"].includes(String(property)))
        return () => {
          throw new TypeError("immutable");
        };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

class PrivateResponse extends Response {
  #url: string;
  #redirected: boolean;
  #headers: Headers;

  constructor(
    body: BodyInit | null,
    init: ResponseInit,
    url: string,
    redirected: boolean,
  ) {
    super(body, init);
    this.#url = url;
    this.#redirected = redirected;
    this.#headers = immutable(super.headers);
  }

  override get url(): string {
    return this.#url;
  }
  override get redirected(): boolean {
    return this.#redirected;
  }
  override get type(): ResponseType {
    return "basic";
  }
  override get headers(): Headers {
    return this.#headers;
  }
  override clone(): Response {
    const copy = super.clone();
    return new PrivateResponse(
      copy.body,
      {
        status: copy.status,
        statusText: copy.statusText,
        headers: copy.headers,
      },
      this.url,
      this.redirected,
    );
  }
}

async function connect(
  vm: Connector,
  port: number,
  signal: AbortSignal,
): Promise<Duplex> {
  signal.throwIfAborted();
  let abort: () => void;
  try {
    return await new Promise<Duplex>((resolve, reject) => {
      abort = () => reject(signal.reason);
      signal.addEventListener("abort", abort, { once: true });
      vm.connectPort(port).then((stream) => {
        if (signal.aborted) {
          stream.destroy();
          reject(signal.reason);
        } else resolve(stream);
      }, reject);
    });
  } finally {
    signal.removeEventListener("abort", abort!);
  }
}

function bodyLength(body: BodyInit | null | undefined): number | undefined {
  if (typeof body === "string" || body instanceof URLSearchParams)
    return Buffer.byteLength(String(body));
  if (body instanceof ArrayBuffer || ArrayBuffer.isView(body))
    return body.byteLength;
  if (body instanceof Blob) return body.size;
  return undefined;
}

export function abortAwareBody(
  source: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): ReadableStream<Uint8Array> {
  const reader = source.getReader();
  return new ReadableStream<Uint8Array>(
    {
      async pull(controller) {
        try {
          signal.throwIfAborted();
          const result = await reader.read();
          signal.throwIfAborted();
          if (result.done) controller.close();
          else controller.enqueue(result.value);
        } catch (error) {
          controller.error(signal.aborted ? signal.reason : error);
        }
      },
      async cancel(reason) {
        try {
          await reader.cancel(reason);
        } catch (error) {
          throw signal.aborted ? signal.reason : error;
        }
      },
    },
    { highWaterMark: 0 },
  );
}

async function exchange(
  vm: Connector,
  port: number,
  request: Request,
  length: number | undefined,
  redirected: boolean,
  mode: ResponseMode,
): Promise<Response> {
  const stream = await connect(vm, port, request.signal);
  return new Promise<Response>((resolve, reject) => {
    const headers: Record<string, string> = {};
    request.headers.forEach((value, name) => {
      headers[name] = value;
    });
    if (length !== undefined && !request.headers.has("content-length"))
      headers["content-length"] = String(length);
    let upload: Readable | undefined;
    let incoming: import("node:http").IncomingMessage | undefined;
    let outgoing: import("node:http").ClientRequest;
    const cleanup = () => {
      request.signal.removeEventListener("abort", abort);
      upload?.destroy();
      stream.destroy();
    };
    const abort = () => {
      const reason = request.signal.reason;
      incoming?.destroy(
        reason instanceof Error ? reason : new Error("Request aborted"),
      );
      outgoing?.destroy();
      cleanup();
      reject(reason);
    };
    try {
      outgoing = httpRequest(
        request.url,
        {
          method: request.method,
          headers,
          createConnection: () => stream,
        },
        (message) => {
          incoming = message;
          message.once("end", cleanup);
          message.once("close", cleanup);
          const responseHeaders = new Headers();
          for (let index = 0; index < message.rawHeaders.length; index += 2) {
            responseHeaders.append(
              message.rawHeaders[index]!,
              message.rawHeaders[index + 1]!,
            );
          }
          const status = message.statusCode!;
          const noBody =
            (mode === "fetch" && request.method === "HEAD") ||
            [204, 205, 304].includes(status);
          try {
            const body = noBody
              ? null
              : abortAwareBody(
                  Readable.toWeb(message) as ReadableStream<Uint8Array>,
                  request.signal,
                );
            const response =
              mode === "blaxel-h2"
                ? new Response(body, { status, headers: responseHeaders })
                : new PrivateResponse(
                    body,
                    {
                      status,
                      statusText: message.statusMessage,
                      headers: responseHeaders,
                    },
                    request.url,
                    redirected,
                  );
            if (noBody) message.resume();
            resolve(response);
          } catch (error) {
            message.destroy();
            cleanup();
            reject(error);
          }
        },
      );
      outgoing.once("error", (error) => {
        incoming?.destroy(error);
        cleanup();
        reject(error);
      });
      request.signal.addEventListener("abort", abort, { once: true });
      if (request.signal.aborted) {
        abort();
        return;
      }
      if (request.body) {
        upload = Readable.fromWeb(
          request.body as import("node:stream/web").ReadableStream<Uint8Array>,
        );
        upload.once("error", (error) => outgoing.destroy(error));
        upload.pipe(outgoing);
      } else outgoing.end();
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

export async function privateFetch(
  vm: Connector,
  port: number,
  path = "/",
  init?: RequestInit,
  mode: ResponseMode = "fetch",
): Promise<Response> {
  init = normalizeHeaders(init, mode);
  if (!Number.isInteger(port) || port < 1 || port > 65535)
    throw new RangeError("port must be an integer from 1 through 65535");
  const url = `http://localhost:${port}${path.startsWith("/") ? path : `/${path}`}`;
  let request = new Request(url, init);
  if (request.integrity)
    throw new TypeError(
      "Private fetch integrity verification is not implemented",
    );
  let body = init?.body;
  for (let redirects = 0; ; redirects++) {
    const response = await exchange(
      vm,
      port,
      request,
      bodyLength(body),
      redirects > 0,
      mode,
    );
    const location = response.headers.get("location");
    if (
      mode === "blaxel-h2" ||
      ![301, 302, 303, 307, 308].includes(response.status) ||
      location === null ||
      request.redirect === "manual"
    )
      return response;
    await response.body?.cancel();
    if (request.redirect === "error")
      throw new TypeError("Redirect encountered with redirect mode error");
    if (redirects >= 20) throw new TypeError("Too many redirects");
    const destination = new URL(location, request.url);
    if (destination.origin !== new URL(url).origin)
      throw new TypeError(
        "Redirect outside the requested private sandbox service is unsupported",
      );
    let method = request.method;
    const headers = new Headers(request.headers);
    if (
      ([301, 302].includes(response.status) && method === "POST") ||
      (response.status === 303 && !["GET", "HEAD"].includes(method))
    ) {
      method = "GET";
      body = undefined;
      for (const name of [
        "content-length",
        "content-type",
        "content-encoding",
        "content-language",
        "content-location",
      ])
        headers.delete(name);
    } else if (body instanceof ReadableStream) {
      throw new TypeError(
        "Cannot replay a streaming request body after redirect",
      );
    }
    request = new Request(destination, {
      ...init,
      method,
      body,
      headers,
      signal: request.signal,
    });
  }
}
