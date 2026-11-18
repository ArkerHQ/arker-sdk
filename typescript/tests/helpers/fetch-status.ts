const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** The status and body of a request, waiting out the moments a new rule or server takes to apply. */
export async function fetchStatus(
  url: string,
  init: { headers?: Record<string, string>; body?: BodyInit; method?: string } = {},
): Promise<[number, string]> {
  let last: [number, string] = [0, ""];
  for (let attempt = 0; attempt < 20; attempt++) {
    try {
      const response = await fetch(url, { ...init, redirect: "manual", signal: AbortSignal.timeout(15_000) });
      last = [response.status, await response.text()];
      if (response.status !== 502 && response.status !== 503) return last;
    } catch {
      // not reachable yet
    }
    await sleep(1_000);
  }
  return last;
}

/** Whether `url` is refused before `deadline`: Arker applies a rule's expiry within seconds of it. */
export async function refusedBy(url: string, deadline: number): Promise<boolean> {
  while ((await fetchStatus(url))[0] === 200 && Date.now() < deadline) await sleep(1_000);
  return [401, 403].includes((await fetchStatus(url))[0]);
}

export function multipart(name: string, data: Uint8Array): { body: Blob; contentType: string } {
  const boundary = crypto.randomUUID().replaceAll("-", "");
  const head = new TextEncoder().encode(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: application/octet-stream\r\n\r\n`,
  );
  const tail = new TextEncoder().encode(`\r\n--${boundary}--\r\n`);
  return { body: new Blob([head, data as Uint8Array<ArrayBuffer>, tail]), contentType: `multipart/form-data; boundary=${boundary}` };
}
