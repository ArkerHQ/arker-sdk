/**
 * iOS QA example for a caller-selected source VM.
 *
 * Smoke only:
 *   ARKER_API_KEY=ark_live_... IOS_QA_SOURCE_VM=<source-name> IOS_QA_SMOKE_ONLY=1 bun run example:ios-qa
 *
 * Smoke with a simulator screenshot:
 *   ARKER_API_KEY=ark_live_... IOS_QA_SOURCE_VM=<source-name> IOS_QA_SMOKE_ONLY=1 IOS_QA_SCREENSHOT=1 bun run example:ios-qa
 *
 * XCTest:
 *   ARKER_API_KEY=ark_live_... IOS_QA_SOURCE_VM=<source-name> IOS_QA_WORKDIR=/Users/arker/app IOS_QA_SCHEME=MyApp bun run example:ios-qa
 */

import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { Arker, type CompletedRunResult, type VM } from "../src/index.js";

const decoder = new TextDecoder();

function env(name: string, fallback?: string): string {
  const value = process.env[name]?.trim();
  return value && value.length > 0 ? value : (fallback ?? "");
}

function requiredEnv(name: string): string {
  const value = env(name);
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function envInt(name: string, fallback: number): number {
  const raw = env(name);
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value)) throw new Error(`${name} must be an integer`);
  return value;
}

function sh(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function text(bytes: Uint8Array): string {
  return decoder.decode(bytes);
}

function vmMetadata(vm: VM): string {
  const fields = [
    vm.state ? `state=${vm.state}` : "",
    vm.provider ? `provider=${vm.provider}` : "",
    vm.region ? `region=${vm.region}` : "",
    vm.root_source_vm_name ? `root=${vm.root_source_vm_name}` : "",
  ].filter(Boolean);
  return fields.length > 0 ? ` (${fields.join(", ")})` : "";
}

// Synchronous run() auto-polls a backgrounded run (one that outlived its sync
// window) to completion under the hood, so the result is always the terminal
// run — no manual background polling needed here. `label` is retained for
// call-site readability.
async function runAndWait(vm: VM, command: string, timeout: number, _label: string): Promise<CompletedRunResult> {
  return vm.run(command, { timeout });
}

function writeRunOutput(result: CompletedRunResult): void {
  if (result.stdout.length > 0) process.stdout.write(result.stdout);
  if (result.stderr.length > 0) process.stderr.write(result.stderr);
}

async function runOrThrow(vm: VM, command: string, timeout: number, label: string): Promise<CompletedRunResult> {
  const result = await runAndWait(vm, command, timeout, label);
  writeRunOutput(result);
  if (result.state !== "completed") {
    throw new Error(`${label} ${result.state}${result.failReason ? `: ${result.failReason}` : ""}`);
  }
  if (result.exitCode !== 0) throw new Error(`${label} exited ${result.exitCode}`);
  return result;
}

async function tryCollect(vm: VM, remotePath: string, localPath: string): Promise<void> {
  try {
    const bytes = await vm.sync(remotePath);
    await writeFile(localPath, bytes);
    console.log(`saved ${localPath} (${bytes.byteLength} bytes)`);
  } catch (error) {
    console.warn(`could not collect ${remotePath}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function collectRequired(vm: VM, remotePath: string, localPath: string): Promise<void> {
  const bytes = await vm.sync(remotePath);
  await writeFile(localPath, bytes);
  console.log(`saved ${localPath} (${bytes.byteLength} bytes)`);
}

async function collectSimulatorScreenshot(vm: VM, artifactDir: string, device: string, timeout: number): Promise<void> {
  await mkdir(artifactDir, { recursive: true });
  await runOrThrow(
    vm,
    [
      `DEVICE=${sh(device)}`,
      "rm -f /tmp/ios-qa-screenshot.png",
      '(xcrun simctl boot "$DEVICE" || true)',
      'xcrun simctl bootstatus "$DEVICE" -b',
      'xcrun simctl io "$DEVICE" screenshot /tmp/ios-qa-screenshot.png',
      "ls -lh /tmp/ios-qa-screenshot.png",
    ].join(" && "),
    timeout,
    "simulator screenshot",
  );
  await collectRequired(vm, "/tmp/ios-qa-screenshot.png", join(artifactDir, "screenshot.png"));
}

async function collectFailureArtifacts(vm: VM, artifactDir: string): Promise<void> {
  await mkdir(artifactDir, { recursive: true });
  await runAndWait(vm, "xcrun simctl io booted screenshot /tmp/ios-qa-failure.png || true", 60, "failure screenshot")
    .catch(() => undefined);
  await tryCollect(vm, "/tmp/ios-qa-failure.png", join(artifactDir, "failure.png"));
}

async function collectXcresult(vm: VM, artifactDir: string): Promise<void> {
  await mkdir(artifactDir, { recursive: true });
  await runAndWait(
    vm,
    "if [ -d /tmp/ios-qa.xcresult ]; then tar -czf /tmp/ios-qa.xcresult.tgz -C /tmp ios-qa.xcresult; fi",
    120,
    "xcresult archive",
  );
  await tryCollect(vm, "/tmp/ios-qa.xcresult.tgz", join(artifactDir, "ios-qa.xcresult.tgz"));
}

const ar = new Arker({
  provider: env("ARKER_PROVIDER", "aws"),
  region: env("ARKER_REGION", "us-west-2"),
  apiKey: process.env.ARKER_API_KEY,
  baseUrl: env("ARKER_BASE_URL") || undefined,
});

const smokeOnly = env("IOS_QA_SMOKE_ONLY") === "1";
const captureScreenshot = env("IOS_QA_SCREENSHOT") === "1";
const artifactDir = env("IOS_QA_ARTIFACT_DIR", "ios-qa-artifacts");
const keepVm = env("IOS_QA_KEEP_VM") === "1";
const source = requiredEnv("IOS_QA_SOURCE_VM");
const vcpu = envInt("IOS_QA_VCPU", 4);
const memoryMib = envInt("IOS_QA_MEMORY_MIB", 8192);
const screenshotDevice = env("IOS_QA_SCREENSHOT_DEVICE", "iPhone 17");
const screenshotTimeout = envInt("IOS_QA_SCREENSHOT_TIMEOUT_SECS", 300);

let vm: VM | undefined;

try {
  vm = await ar.fork({
    source_vm_name: source,
    name: `ios-qa-${Date.now()}`,
    resources: { vcpu, memory_mib: memoryMib },
  });
  console.log(`forked ${vm.id} from ${source}${vmMetadata(vm)}`);

  await runOrThrow(
    vm,
    "xcodebuild -version && xcrun simctl list devices available | head -80",
    180,
    "iOS tooling readiness",
  );

  if (smokeOnly) {
    if (captureScreenshot) {
      await collectSimulatorScreenshot(vm, artifactDir, screenshotDevice, screenshotTimeout);
    }
    console.log("smoke passed");
  } else {
    const workdir = requiredEnv("IOS_QA_WORKDIR");
    const scheme = requiredEnv("IOS_QA_SCHEME");
    const destination = env("IOS_QA_DESTINATION", `platform=iOS Simulator,name=${screenshotDevice}`);
    const extraArgs = env("IOS_QA_XCODEBUILD_ARGS");
    const timeout = envInt("IOS_QA_TIMEOUT_SECS", 1800);
    const testCommand =
      env("IOS_QA_TEST_COMMAND") ||
      [
        `cd ${sh(workdir)}`,
        "rm -rf /tmp/ios-qa.xcresult",
        `xcodebuild test ${extraArgs} -scheme ${sh(scheme)} -destination ${sh(destination)} -resultBundlePath /tmp/ios-qa.xcresult`,
      ].join(" && ");

    const test = await runAndWait(vm, testCommand, timeout, "xcodebuild test");
    writeRunOutput(test);

    if (test.state !== "completed" || test.exitCode !== 0) {
      await collectFailureArtifacts(vm, artifactDir);
      throw new Error(
        test.state === "completed"
          ? `xcodebuild test exited ${test.exitCode}`
          : `xcodebuild test ${test.state}${test.failReason ? `: ${test.failReason}` : ""}`,
      );
    }

    await collectXcresult(vm, artifactDir);
    console.log("iOS QA passed");
  }
} finally {
  if (vm && !keepVm) {
    try {
      await vm.delete();
      console.log(`deleted ${vm.id}`);
    } catch (error) {
      console.warn(`could not delete ${vm.id}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}
