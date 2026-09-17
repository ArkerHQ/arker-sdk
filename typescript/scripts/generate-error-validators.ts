import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import standaloneCode from 'ajv/dist/standalone/index.js';
import { readFileSync, writeFileSync } from 'node:fs';

const root = new URL('../../', import.meta.url);
const contract = JSON.parse(readFileSync(new URL('openapi.json', root), 'utf8'));
const ajv = new Ajv2020({strict: false, inlineRefs: false, code: {source: true, esm: true}});
addFormats(ajv);
ajv.addSchema({...contract, $id: 'arker'});
const names = {validateHttpError: 'ErrorBody', validateFileError: 'SyncEntryError'};
for (const [name, schema] of Object.entries(names)) {
  ajv.addSchema({$id: name, $ref: `arker#/components/schemas/${schema}`});
}
writeFileSync(new URL('typescript/src/generated/error-validators.js', root),
  '// Generated from openapi.json. Do not edit.\n' + standaloneCode(ajv, Object.fromEntries(Object.keys(names).map(name => [name,name]))));
writeFileSync(new URL('typescript/src/generated/error-validators.d.ts', root),
  'import type { components } from "./api-types";\n' + Object.entries(names).map(([name,schema]) =>
    `export function ${name}(value: unknown): value is components["schemas"]["${schema}"];`).join('\n') + '\n');

const result = await Bun.build({
  entrypoints: [new URL('typescript/src/generated/error-validators.js', root).pathname],
  target: 'browser', format: 'esm', minify: true,
});
if (!result.success) throw new AggregateError(result.logs, 'Error validator bundling failed');
writeFileSync(new URL('typescript/src/generated/error-validators.js', root),
  '// Generated from openapi.json. Do not edit.\n' + await result.outputs[0]!.text());
