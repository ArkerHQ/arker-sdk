import type { VM } from "../index.js";
import type { UniversalSandbox } from "./common.js";
import { shellQuote } from "./common.js";
import source from "./modal-defaults-guest.generated.js";
import { creationDefaults } from "./modal-recipes.js";

export const MODAL_DEFAULTS_MARKER = "computesdk-modal-defaults";
const path = "/root/.arker-modal/defaults.json";

function vm(box: UniversalSandbox): VM {
  return (box as UniversalSandbox & { getInstance(): VM }).getInstance();
}
async function guard(box: UniversalSandbox, operation: string) {
  const result = await box.runCommand(`python3 -c ${shellQuote(source)} ${operation}`);
  if (result.exitCode) throw new Error("Cannot secure Modal creation defaults");
}
export async function writeDefaults(box: UniversalSandbox, defaults: ReturnType<typeof creationDefaults>) {
  await guard(box, "prepare");
  await vm(box).sync(path, JSON.stringify(defaults));
  await guard(box, "seal");
}
export async function readDefaults(box: UniversalSandbox): Promise<ReturnType<typeof creationDefaults>> {
  if (!vm(box).metadata?.[MODAL_DEFAULTS_MARKER]) return { env: {}, workdir: "/" };
  await guard(box, "check");
  const value = JSON.parse(new TextDecoder().decode(await vm(box).sync(path)));
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(key => key !== "env" && key !== "workdir")) {
    throw new Error("Malformed Modal creation defaults");
  }
  return creationDefaults(value);
}

export async function prepareExec(box: UniversalSandbox, argv: string[], env: Record<string, string>, workdir?: string) {
  if (!Object.keys(env).length && !vm(box).metadata?.[MODAL_DEFAULTS_MARKER]) return { argv, name: undefined };
  const name = `exec-${crypto.randomUUID().replaceAll("-", "")}.json`;
  try {
    await guard(box, `prepare ${name}`);
    await vm(box).sync(`/root/.arker-modal/${name}`, JSON.stringify({ argv, env, workdir: workdir ?? null, defaults: !!vm(box).metadata?.[MODAL_DEFAULTS_MARKER] }));
    await guard(box, `seal ${name}`);
  } catch (error) {
    await discardExec(box, name, error);
    throw error;
  }
  return { argv: ["python3", "-c", source, "exec", name], name };
}

export async function discardExec(box: UniversalSandbox, name: string | undefined, primary?: unknown) {
  if (name === undefined) return;
  try { await guard(box, `remove ${name}`); }
  catch (cleanup) {
    if (primary !== undefined) throw new AggregateError([primary, cleanup], `Modal control cleanup failed for VM ${vm(box).id}`);
    throw cleanup;
  }
}
