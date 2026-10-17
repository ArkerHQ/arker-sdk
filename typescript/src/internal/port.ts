import { Duplex } from "node:stream";
import WebSocket from "ws";

class PortStream extends Duplex {
  private readEof = false;
  private writeEof = false;

  constructor(private readonly socket: WebSocket) {
    super({ allowHalfOpen: true });
    socket.on("message", (data, binary) => {
      if (this.readEof) {
        this.destroy(new Error("Private port received data after EOF"));
      } else if (binary) {
        const bytes = Array.isArray(data) ? Buffer.concat(data) : Buffer.from(data as Buffer);
        if (bytes.length > 65536) {
          this.destroy(new Error("Private port frame exceeds 65536 bytes"));
          return;
        }
        if (!this.push(bytes)) socket.pause();
      } else if (data.toString() === "eof") {
        this.readEof = true;
        this.push(null);
      } else {
        this.destroy(new Error("Unexpected private port text frame"));
      }
    });
    socket.on("error", (error) => this.destroy(error));
    socket.on("close", () => {
      if (!this.readEof || !this.writeEof) this.destroy(new Error("Private port closed before EOF"));
    });
  }

  override _read(): void { this.socket.resume(); }

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    let offset = 0;
    const next = (error?: Error): void => {
      if (error) { callback(error); return; }
      if (offset >= chunk.length) { callback(); return; }
      const frame = chunk.subarray(offset, offset + 65536);
      offset += frame.length;
      this.socket.send(frame, { binary: true }, next);
    };
    next();
  }

  override _final(callback: (error?: Error | null) => void): void {
    this.writeEof = true;
    this.socket.send("eof", callback);
  }

  override _destroy(error: Error | null, callback: (error: Error | null) => void): void {
    this.socket.close();
    callback(error);
  }
}

export function connectPortStream(
  url: string,
  headers: Record<string, string>,
  timeoutMs: number,
  parseError: (body: string, status: number) => Error,
): Promise<Duplex> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url, "vm-port.v1", {
      headers, handshakeTimeout: timeoutMs, followRedirects: false, maxPayload: 65536,
    });
    socket.once("error", reject);
    socket.once("open", () => {
      const stream = new PortStream(socket);
      stream.once("error", reject);
      socket.removeListener("error", reject);
      // Upgrade and its first frames can arrive in one socket read. Deliver any
      // immediate protocol failure to the connection promise, not an unowned stream.
      setImmediate(() => {
        if (stream.destroyed) { reject(stream.errored ?? new Error("Private port closed during upgrade")); return; }
        resolve(stream);
        queueMicrotask(() => stream.removeListener("error", reject));
      });
    });
    socket.once("unexpected-response", (_request, response) => {
      let body = "";
      const timer = setTimeout(() => {
        response.destroy();
        socket.terminate();
        reject(new Error("Private port upgrade response timed out"));
      }, timeoutMs);
      response.on("data", (chunk: Buffer) => {
        body += chunk.toString();
        if (Buffer.byteLength(body) > 65536) {
          response.destroy(new Error("Private port upgrade error exceeds 65536 bytes"));
        }
      });
      response.once("error", (error) => {
        clearTimeout(timer);
        reject(error);
        socket.terminate();
      });
      response.once("end", () => {
        clearTimeout(timer);
        reject(parseError(body, response.statusCode ?? 500));
        socket.terminate();
      });
    });
  });
}
