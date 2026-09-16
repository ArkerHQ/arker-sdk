import type { components } from "../src/generated/api-types.js";
import type {
  Filesystem,
  ListFilesystemsResponse,
  ListOrgRunsResponse,
  ListRunsResponse,
  ListSessionsResponse,
  ListVmsResponse,
  OrgRunListRow,
  Run,
  RunSummary,
  Session,
  SyncReadOperationRequest,
  SyncWriteOperationRequest,
  SyncWriteEntry,
  Vm,
} from "../src/index.js";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
  (<Value>() => Value extends Right ? 1 : 2)
    ? true
    : false;
type Expect<Value extends true> = Value;
type Schema<Name extends keyof components["schemas"]> = components["schemas"][Name];

type ContractTypes = [
  Expect<Equal<Session, Schema<"Session">>>,
  Expect<Equal<Vm, Schema<"Vm">>>,
  Expect<Equal<ListVmsResponse, Schema<"ListVmsResponse">>>,
  Expect<Equal<ListSessionsResponse, Schema<"ListSessionsResponse">>>,
  Expect<Equal<Run, Schema<"Run">>>,
  Expect<Equal<RunSummary, Schema<"RunSummary">>>,
  Expect<Equal<ListRunsResponse, Schema<"ListRunsResponse">>>,
  Expect<Equal<Filesystem, Schema<"Filesystem">>>,
  Expect<Equal<ListFilesystemsResponse, Schema<"ListFilesystemsResponse">>>,
  Expect<Equal<OrgRunListRow, Schema<"OrgRunListRow">>>,
  Expect<Equal<ListOrgRunsResponse, Schema<"ListOrgRunsResponse">>>,
  Expect<Equal<SyncReadOperationRequest, Schema<"SyncReadOperationRequest">>>,
  Expect<Equal<SyncWriteOperationRequest, Schema<"SyncWriteOperationRequest">>>,
  Expect<Equal<SyncWriteEntry, Schema<"SyncWriteEntry">>>,
];

declare const contractTypes: ContractTypes;
void contractTypes;

// @ts-expect-error quota measurements must be structured, never an arbitrary scalar.
const invalidMeasurements: Schema<"QuotaMeasurements"> = "invalid";
void invalidMeasurements;
