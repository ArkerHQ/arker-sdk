import ts from "typescript";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { implementationFingerprint, runtimeInfo, verifiedScenarios } from "./compat-evidence.js";

const roots = [
  ["e2b", "e2b", "CommandHandle", "../src/e2b.js"],
  ["e2b", "e2b", "CommandInputHandle", "../src/e2b.js"],
  ["e2b", "e2b", "Sandbox", "../src/e2b.js"],
  ["e2b", "e2b", "Template", "../src/e2b.js"],
  ["e2b", "e2b", "Volume", "../src/e2b.js"],
  ["e2b", "e2b", "Secret", "../src/e2b.js"],
  ["daytona", "@daytona/sdk", "Daytona", "../src/daytona.js"],
  ["daytona", "@daytona/sdk", "Daytona", "../src/daytona.js", "volume"],
  ["daytona", "@daytona/sdk", "Sandbox", "../src/daytona.js"],
  ["daytona", "@daytona/sdk", "Image", "../src/daytona.js"],
  ["modal", "modal", "ModalClient", "../src/modal.js"],
  ["modal", "modal", "ContainerProcess", "../src/modal.js"],
  ["modal", "modal", "Sandbox", "../src/modal.js"],
  ["modal", "modal", "Image", "../src/modal.js"],
  ["modal", "modal", "App", "../src/modal.js"],
  ["modal", "modal", "Volume", "../src/modal.js"],
  ["modal", "modal", "Secret", "../src/modal.js"],
  ["blaxel", "@blaxel/core", "SandboxInstance", "../src/blaxel.js"],
  ["blaxel", "@blaxel/core", "DriveInstance", "../src/blaxel.js"],
  ["blaxel", "@blaxel/core", "VolumeInstance", "../src/blaxel.js"],
  ["blaxel", "@blaxel/core", "Snapshot", "../src/blaxel.js"],
  ["blaxel", "@blaxel/core", "ImageInstance", "../src/blaxel.js"],
] as const;

const filename = resolve("scripts/__compat_inventory__.ts");
const source = roots.map(([, module, name, shim], index) => (
  `import { ${name} as Native${index} } from ${JSON.stringify(module)};\n` +
  `import { ${name} as Shim${index} } from ${JSON.stringify(shim)};\n`
)).join("\n");
const options: ts.CompilerOptions = {
  target: ts.ScriptTarget.ESNext,
  module: ts.ModuleKind.NodeNext,
  moduleResolution: ts.ModuleResolutionKind.NodeNext,
  skipLibCheck: true,
  strict: true,
};
const host = ts.createCompilerHost(options);
const originalGetSourceFile = host.getSourceFile.bind(host);
host.getSourceFile = (path, languageVersion, onError, shouldCreate) => (
  path === filename ? ts.createSourceFile(filename, source, languageVersion, true) :
    originalGetSourceFile(path, languageVersion, onError, shouldCreate)
);
const program = ts.createProgram([filename], options, host);
const checker = program.getTypeChecker();
const entry = program.getSourceFile(filename)!;
const types = new Map<string, { instance: ts.Type; constructor: ts.Type }>();
for (const statement of entry.statements) {
  if (!ts.isImportDeclaration(statement)) continue;
  const bindings = statement.importClause?.namedBindings;
  if (!bindings || !ts.isNamedImports(bindings)) continue;
  for (const binding of bindings.elements) {
    const alias = checker.getSymbolAtLocation(binding.name)!;
    const symbol = checker.getAliasedSymbol(alias);
    types.set(binding.name.text, {
      instance: checker.getDeclaredTypeOfSymbol(symbol),
      constructor: checker.getTypeOfSymbolAtLocation(symbol, binding),
    });
  }
}

type Row = {
  id: string;
  provider: string;
  api: string;
  kind: string;
  signatures: string[];
  declared_by_shim: boolean;
  shim_signatures: string[];
  tested_signatures: string[];
  live_signatures: string[];
  verified_scenarios: string[];
  status: string;
};
const rows = new Map<string, Row>();
function inventory(provider: string, prefix: string, type: ts.Type, shim: ts.Type | undefined, depth = 0): number {
  let inspected = 0;
  for (const property of checker.getPropertiesOfType(type)) {
    const name = property.name;
    if (name.startsWith("_") || name.startsWith("#") || name === "prototype") continue;
    const declaration = property.valueDeclaration ?? property.declarations?.[0];
    if (!declaration) continue;
    if (ts.canHaveModifiers(declaration) && ts.getModifiers(declaration)?.some((modifier) => (
      modifier.kind === ts.SyntaxKind.PrivateKeyword || modifier.kind === ts.SyntaxKind.ProtectedKeyword
    ))) continue;
    const value = checker.getTypeOfSymbolAtLocation(property, declaration);
    const signatures = checker.getSignaturesOfType(value, ts.SignatureKind.Call);
    const api = `${prefix}.${name}`;
    const id = `typescript:${provider}:async:${api}`;
    const shimProperty = shim ? checker.getPropertyOfType(shim, name) : undefined;
    inspected++;
    rows.set(id, {
      id, provider, api,
      kind: signatures.length ? "method" : "property",
      signatures: signatures.map((signature) => checker.signatureToString(signature)),
      declared_by_shim: !!shimProperty,
      shim_signatures: shimProperty ? checker.getSignaturesOfType(
        checker.getTypeOfSymbolAtLocation(shimProperty, shimProperty.valueDeclaration ?? declaration),
        ts.SignatureKind.Call,
      ).map((signature) => checker.signatureToString(signature)) : [],
      tested_signatures: [], live_signatures: [], verified_scenarios: [],
      status: "unverified",
    });
    if (!signatures.length && depth < 2 && value.getFlags() & ts.TypeFlags.Object) {
      const filename = value.symbol?.declarations?.[0]?.getSourceFile().fileName;
      if (!filename || !/node_modules/.test(filename) || /typescript\/lib\/lib\./.test(filename)) continue;
      const shimValue = shimProperty ? checker.getTypeOfSymbolAtLocation(shimProperty, declaration) : undefined;
      inventory(provider, api, value, shimValue, depth + 1);
    }
  }
  return inspected;
}

const missingRoots: string[] = [];
for (const [index, [provider, , name, , qualifiedProperty]] of roots.entries()) {
  let inspected = 0;
  const native = types.get(`Native${index}`)!;
  const shim = types.get(`Shim${index}`);
  if (qualifiedProperty) {
    const nativeProperty = checker.getPropertyOfType(native.instance, qualifiedProperty);
    const shimProperty = shim ? checker.getPropertyOfType(shim.instance, qualifiedProperty) : undefined;
    if (nativeProperty) {
      const declaration = nativeProperty.valueDeclaration ?? nativeProperty.declarations?.[0]!;
      const nativeType = checker.getTypeOfSymbolAtLocation(nativeProperty, declaration);
      const shimType = shimProperty ? checker.getTypeOfSymbolAtLocation(shimProperty, shimProperty.valueDeclaration ?? declaration) : undefined;
      inspected = inventory(provider, `${name}.${qualifiedProperty}`, nativeType, shimType);
    }
  } else {
    inspected = inventory(provider, name, native.instance, shim?.instance)
      + inventory(provider, `${name}.static`, native.constructor, shim?.constructor);
  }
  if (inspected === 0) missingRoots.push(`${provider}:${name}${qualifiedProperty ? `.${qualifiedProperty}` : ""}`);
}
const diagnostics = ts.getPreEmitDiagnostics(program).filter((diagnostic) => diagnostic.file?.fileName === filename);
const vendorVersions: Record<string, string> = {};
for (const [, module] of roots) {
  if (vendorVersions[module]) continue;
  const path = resolve("node_modules", module, "package.json");
  vendorVersions[module] = JSON.parse(await readFile(path, "utf8")).version;
}
const fingerprint = await implementationFingerprint();
const evidence: any[] = [];
for (let index = 0; index < process.argv.length; index++) {
  if (process.argv[index] === "--evidence") {
    const text = await readFile(process.argv[++index]!, "utf8");
    evidence.push(...text.split("\n").filter(Boolean).map((line) => JSON.parse(line)));
  }
}
for (const item of evidence) {
  if (item.signatures !== undefined && (!item.signatures || Array.isArray(item.signatures) ||
    typeof item.signatures !== "object" || !Object.entries(item.signatures).every(([api, values]) =>
      Array.isArray(item.apis) && item.apis.includes(api) && Array.isArray(values) &&
      values.every((value) => typeof value === "string" && value.trim())))) {
    throw new Error("Invalid compatibility signature evidence");
  }
}
const rejectedEvidence = evidence.filter((item) => {
  const module = roots.find(([provider]) => provider === item.provider)?.[1];
  return item.implementation_fingerprint !== fingerprint || !module || item.vendor_version !== vendorVersions[module];
}).length;
for (const row of rows.values()) {
  const module = roots.find(([provider]) => provider === row.provider)![1];
  const providerEvidence = evidence.filter((item) => item.provider === row.provider);
  row.verified_scenarios = verifiedScenarios(row.id, providerEvidence, vendorVersions[module]!, fingerprint);
  row.status = row.verified_scenarios.length ? "scenario_verified" : "unverified";
  row.tested_signatures = row.signatures.filter((signature) => providerEvidence.some((item) =>
    item.implementation_fingerprint === fingerprint && item.vendor_version === vendorVersions[module] &&
    item.outcome === "passed" && item.apis.includes(row.id) && item.signatures?.[row.id]?.includes(signature)));
  row.live_signatures = row.signatures.filter((signature) => verifiedScenarios(row.id, providerEvidence.map((item) => ({
    ...item, apis: item.signatures?.[row.id]?.includes(signature) ? item.apis : [],
  })), vendorVersions[module]!, fingerprint).length > 0);
}
const counts: Record<string, number> = {};
const scopeIndex = process.argv.indexOf("--scope");
const selectedScope = scopeIndex < 0 ? "expanded" : process.argv[scopeIndex + 1];
if (selectedScope !== "expanded" && selectedScope !== "core-sandbox") throw new Error("Unknown compatibility scope");
const scopeContract = JSON.parse(await readFile(new URL("../../compatibility/api-scope.json", import.meta.url), "utf8")) as {
  version: number; exclusions: { provider: string; pattern: string; reason: string }[];
};
if (scopeContract.version !== 1) throw new Error("Unsupported API scope contract");
const selectedRows: Row[] = [];
const exclusions: (Row & { exclusion_reason: string })[] = [];
for (const row of rows.values()) {
  const rule = scopeContract.exclusions.find((rule) => rule.provider === row.provider && new RegExp(rule.pattern).test(row.api));
  if (rule) exclusions.push({ ...row, exclusion_reason: rule.reason });
  if (selectedScope === "expanded" || !rule) selectedRows.push(row);
}
for (const row of selectedRows) counts[row.status] = (counts[row.status] ?? 0) + 1;
const report = {
  schema_version: 1,
  language: "typescript",
  selected_scope: selectedScope,
  scope_exclusions: exclusions,
  runtime: runtimeInfo(),
  scope: "Public sandbox/client methods and properties, static methods, and connected service objects to depth 2; exposed declarations do not count as behavioral verification.",
  vendor_versions: vendorVersions,
  implementation_fingerprint: fingerprint,
  complete_inventory: selectedRows.length > 0 && diagnostics.length === 0 && missingRoots.length === 0,
  missing_roots: missingRoots,
  diagnostics: diagnostics.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")),
  counts,
  rejected_evidence: rejectedEvidence,
  signature_counts: {
    vendor: selectedRows.reduce((total, row) => total + row.signatures.length, 0),
    shim_declared: selectedRows.reduce((total, row) => total + row.shim_signatures.length, 0),
    tested: selectedRows.reduce((total, row) => total + row.tested_signatures.length, 0),
    live: selectedRows.reduce((total, row) => total + row.live_signatures.length, 0),
  },
  full_api_equivalence: false,
  apis: selectedRows.sort((a, b) => a.id.localeCompare(b.id)),
};
const output = JSON.stringify(report, null, 2) + "\n";
const outputIndex = process.argv.indexOf("--output");
if (outputIndex >= 0) await writeFile(process.argv[outputIndex + 1]!, output);
else process.stdout.write(output);
if (process.argv.includes("--fail-on-unverified") && (!report.complete_inventory || rejectedEvidence > 0 || selectedRows.some((row) =>
  row.status === "unverified" || row.signatures.length !== row.live_signatures.length))) {
  process.exitCode = 1;
}
