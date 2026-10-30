import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

type Provider = "arker" | "blaxel" | "daytona" | "e2b" | "modal";
type LiveRouter = {
  directory: string;
  setProvider(provider: Provider): Promise<void>;
};

// Run sequentially: the router and credentials are process-wide environment state.
export async function withLiveRouter(
  credentials: readonly string[],
  run: (router: LiveRouter) => Promise<void>,
): Promise<void> {
  const missing = credentials.filter((name) => !process.env[name]?.trim());
  if (missing.length) throw new Error(`Missing live credentials/config: ${missing.join(", ")}`);
  const previous = new Map(
    ["COMPUTESDK_ROUTER_CONFIG", ...credentials].map((name) => [name, process.env[name]]),
  );
  const directory = await mkdtemp(join(tmpdir(), "compat-live-router-"));
  let failed = false;
  try {
    await run({
      directory,
      async setProvider(provider) {
        const path = join(directory, "router.json");
        await writeFile(path, JSON.stringify({
          version: 1,
          existing: { strategy: "priority", order: [provider] },
          create: { strategy: "priority", order: [provider] },
        }));
        process.env.COMPUTESDK_ROUTER_CONFIG = path;
      },
    });
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    try { await rm(directory, { recursive: true, force: true }); }
    catch (error) {
      if (!failed) throw error;
      console.error("Live-test temporary directory cleanup also failed.");
    }
  }
}
