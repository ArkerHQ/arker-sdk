/**
 * Which URLs answer with a non-error status, given a function that runs a
 * shell command in the sandbox and returns its stdout.
 */
export async function reachable(run: (command: string) => Promise<string>, ...urls: string[]): Promise<Record<string, boolean>> {
  const script = urls.map((url) => `curl -s -o /dev/null -m 8 -w '%{http_code}\\n' ${url}`).join("; ");
  const codes = (await run(`${script}; true`)).split(/\s+/).filter(Boolean);
  if (codes.length !== urls.length) throw new Error(`expected ${urls.length} status codes, got: ${codes.join(" ")}`);
  return Object.fromEntries(urls.map((url, index) => [url, /^[23]/.test(codes[index]!)]));
}
