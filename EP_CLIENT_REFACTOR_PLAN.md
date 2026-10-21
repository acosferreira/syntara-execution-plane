# EP client contracts and Syntara HTTP refactor

## Purpose and scope

Keep one authoritative set of EP wire contracts in `execution-plane-client`.
Syntara owns HTTP delivery, configuration, authentication, TLS, URL encoding,
timeouts, and translation of upstream failures into its API errors. EP owns
persistence, query execution, and resource visibility within its client scope.

This plan combines the read-contract refactor with removal of duplicated query
field lists. It is followup work, not a prerequisite for merging Syntara PR
[#893](https://github.com/syntara-orchestration/syntara/pull/893).
RBAC, project associations, new AO routes, and command-protocol redesign are out
of scope.

## What #893 actually changes

Research compared the PR's actual base `d50a5d38913ed5f4886dc7281e9cae4d9759ff44`
on `feat/ANSTRAT-1803` with head
`e951c392ac17e2608c8820129800998c295087c6`. The base is also their merge base.
This is a base-to-head comparison, not an attribution to any author or model.
The local module-docstring followup plan is an additional uncommitted change.

| Area | Pre-existing at the PR base | Added or changed by #893 |
| --- | --- | --- |
| HTTP transport | `ExecutionPlaneHttpClient`, JWT/mTLS configuration, command submission and shared receipts, dictionary-based reads, HTTP status handling | Full list-query forwarding, `_list_resources`, malformed-JSON error handling, and `_as_page` replacing the array checker |
| Query parameters | AO routes accepted only `limit`; default 20, maximum 100. Internal list client defaulted to 50 | `BaseListParams` integration, local `ExecutionTargetListParams` and `WorkItemListParams`, cursor/sort/total/filter forwarding, internal client default 20 |
| Resource contracts | `ExecutionTargetFacadeRead`, `WorkItemFacadeRead`, local Kubernetes/RHEL placement models, and both manually declared list envelopes | No resource or response-envelope field definitions changed |
| Pagination behavior | AO already documented `resources`, `next`, `prev`, `total`, but constructed responses from arrays with null metadata | Consume upstream envelopes and preserve upstream cursors and totals |
| Validation | Facade resource-model validation already existed; commands used shared Pydantic models | Manual envelope validation, explicit malformed-resource 502 handling, and upstream 422 preservation |
| Authorization | Existing `ProjectScopeFilter` dependencies and access check | Unchanged; no project associations or RBAC implementation |
| OpenAPI tooling | Separate generated EP YAML, exporter, bundle exclusion, drift target, and generated frontend EP contracts | Additional query parameters and prose, documented 502/503 responses, version 0.1.0 to 0.2.0, regenerated frontend contracts |
| Tests/docs | Facade integration tests and existing client tests | Forwarding/error regression coverage and `read-query-contract.md` |

In particular, the existence of `client.py`, duplicate read/placement models,
dictionary return types, and a separate OpenAPI pipeline must not be presented
as regressions introduced by #893. The copied query documentation classes and
manual page checker are new duplication in this PR.

## Is the EP OpenAPI file standard Syntara practice?

It uses OpenAPI 3.1.0, YAML, operation IDs, tags, query parameters, components,
security declarations, and the familiar pagination envelope. Describing one
base filter parameter with bracket syntax in its description also has precedent
in Syntara's users sub-spec. `anyOf` with `null` appears there too; its presence
alone is not an EP-specific problem.

Its maintenance is an explicit, pre-existing exception to the normal domain
workflow:

- Ordinary domain sub-specs are maintained under
  `backend/src/syntara/schemas/<domain>/openapi.yaml`, commonly referencing
  `base/shared-resources.openapi.yaml`, and feed the main bundles.
- The EP facade spec is exported directly from a minimal FastAPI app by
  `make -C backend api-spec-bundle-ep`, rather than maintained as an ordinary
  domain input. FastAPI expands parameters inline and repeats descriptions at
  parameter and schema levels, contributing to the verbose diff.
- Its full paths use `/api/execution_plane/v1`, rather than the usual domain
  paths under a `/api/v1` server prefix. `tools/bundle_openapi.py` explicitly
  excludes it from the main bundles; it has its own drift check and TypeScript
  generation entry point. `tools/ci/validate_api_specs.py` also excludes its
  directory from the ordinary domain validation pass.
- This file describes Syntara's facade and callback, not the standalone EP
  service's `/v1/...` API. Sharing Python contracts will not eliminate the need
  to describe Syntara's actual endpoints, authentication, and error behavior.

The additions in #893 largely reflect newly documented parameters, not a new
OpenAPI architecture. The lengthy endpoint-specific explanation of generic
filtering is new and can be shortened independently. It does not represent a
special filtering protocol.

References: Syntara's `backend/docs/standards/openapi-spec-management.md`, users
sub-spec, `backend/Makefile`, `backend/tools/export_openapi.py`,
`backend/tools/bundle_openapi.py`, and `backend/tools/ci/validate_api_specs.py`.

## Recommended boundary

| Owner | Responsibilities |
| --- | --- |
| `execution-plane-client` | Shared Pydantic commands/receipts, resource wire responses, reusable placement and enum types, pagination envelope, list controls and query capability metadata |
| EP service | ORM models, database aliases, SQL operators, typed filter execution, cursor generation/interpretation, authorization and scoped queries |
| Syntara | HTTP client, credentials/TLS, endpoint paths, query-string encoding, transport errors, public route permissions and any intentional AO response projection |

The shared package remains transport independent. It must import neither
Syntara nor EP service modules and must not acquire HTTP, SQLAlchemy, SQLModel,
Kubernetes, or FastAPI dependencies. Existing command catalog/handler facilities
can remain; this work does not turn the package into an HTTP SDK.

Validation of received JSON against a shared model is expected. Repeating its
schema in manual checks or locally copied classes is what this refactor removes.

## Implementation sequence

### 1. Establish compatibility fixtures before moving contracts

Record current EP and AO request/response examples and generated schemas.
Cover missing versus null fields, unknown fields, enum values, UUIDs,
timestamps, nullable timestamps, placement discriminators, and page metadata.
Retain the current rejection of boolean/negative totals and malformed resource
arrays when replacing `_as_page`; ordinary Pydantic coercion must not silently
weaken these checks.

Document the actual response differences. EP work-item reads include cleanup
state, result omission/size metadata, and completion/revision fields absent from
AO's public read model. AO currently accepts status/backend strings, while EP
uses enums. Shared types must not silently tighten accepted values or expand
AO's public API. EP list queries also omit result payloads intentionally; retain
that behavior.

### 2. Publish shared read contracts from EP

Add client modules such as `resources.py` and `pagination.py` for credential-free
work-item, target, and cluster-binding responses and the generic page envelope.
Move reusable wire enums and placement definitions to the client layer and
reuse them from EP, rather than maintaining copies in both packages.

Use `execution_plane/api/schemas.py` and `api/pagination.py` as the migration
sources. EP can retain compatibility re-exports while imports move. Database
attribute mapping, including cluster binding `source_integration_id -> id` and
`revision -> desired_revision`, belongs to EP's adapter, not the public client's
validation aliases. Test validation against actual EP JSON as well as ORM
conversion.

Choose read-model configuration explicitly. The command `WireModel` forbids
extra fields and freezes values; do not automatically apply it to responses,
where unknown additive fields may need to remain acceptable. Audit placement
validators/defaults before consolidating them: EP, the client commands, and AO
currently define similar types with different strictness.

### 3. Publish authoritative list controls and capabilities

Add `execution_plane_client/list_queries.py` with a shared controls model and
per-resource capability definitions for work items, targets, and cluster
bindings. Define public filter fields, their value kinds/operators, sortable
fields, default sort, page limits, and label support once. Do not infer
queryability from all resource or command fields.

EP should derive `__filterable_fields__`, `__sortable_fields__`, sort validation,
and filter documentation from this contract. Keep storage aliases and SQL
conversion inside EP. Preserve the actual operator matrix rather than
introducing a narrower one during the extraction. Filters on nullable columns
remain distinct from the non-nullable fields permitted for cursor sorting.

An omitted sort must remain distinguishable from an explicit sort. EP resolves
omission to its per-resource default; Syntara forwards omission unchanged.
Represent filter values as their wire strings so comma-separated values,
bracket operators, empty values, and dynamic label keys remain expressible.
Keep raw filters separate from validated controls, so unknown filters still
reach EP and retain its 422 behavior.

Export a small helper that renders compact OpenAPI filter parameters as plain
dictionaries. This avoids both handwritten AO field lists and dynamic query
model generation. The package can describe query values; URL encoding remains
the HTTP caller's responsibility.

### 4. Adopt the released package in Syntara

Update the pinned client revision and lockfile only after the additive package
release is available. Then:

- Have client methods accept shared list controls plus raw filters, instead of
  repeating control arguments/defaults on every list method. Use Pydantic's
  serialization and let httpx encode the parameter mapping.
- Return shared typed resource/page models from HTTP calls, validating the
  received JSON once at the transport boundary. Remove `_as_page`, redundant
  shape checks, dictionary casts, and downstream field-by-field reconstruction
  that only repeats the shared contract.
- Keep command submission on its existing shared-model path. Receipt/message
  correlation remains a transport interaction check.
- Delete AO's two filter-documentation subclasses. Use shared controls with
  `Query()` and attach filter documentation generated from shared metadata via
  `openapi_extra`. Avoid making another local wrapper that repeats the fields.
- Preserve existing 422/502/503 behavior and inspect all current single-resource
  callers when replacing dictionary reads with typed responses.

Syntara's own general `BaseListParams` and `ResourcesResponse` still serve its
native APIs. This work should not impose EP's dependency on unrelated Syntara
domains or replace their shared infrastructure.

### 5. Resolve the public AO projection explicitly

Working assumption: preserve AO's current public response fields. Confirm
whether a later API change should expose EP's full safe response instead.

Where the contracts match, reuse shared types directly. If AO deliberately
retains a narrower response, keep a clearly documented public projection as an
API boundary, not a second upstream validation model. Evaluate using shared
Pydantic field metadata with an explicitly selected subset to preserve accurate
OpenAPI without hand-copying field types. A runtime `exclude` alone is
insufficient if the documented schema still advertises excluded fields.

Do not make that projection choice by silently widening responses during an
internal refactor. Also compare schema requiredness, nullability, enums,
component names/references, and generated TypeScript, not just JSON examples.

### 6. Regenerate contracts and verify the boundary

- Test the built client wheel in isolation: Pydantic-only dependencies, all new
  modules packaged, and no imports from either service.
- Add metadata-to-ORM/OpenAPI parity checks, including aliases, supported
  operators, label support, sortable fields, and per-endpoint defaults.
- Retain PostgreSQL tests for EP pagination/sorting/filtering and AO facade
  integration tests using PostgreSQL with mocked upstream HTTP.
- Retain transport regressions for CSV values, empty `in`/label values,
  timezone plus signs, opaque cursor `+/=`, omitted sort, reserved control
  collisions, unknown filters, filtered totals, and failure translation.
- Regenerate both services' relevant OpenAPI artifacts and Syntara TypeScript
  contracts; run drift, type, and lint checks. Compare API schemas against the
  compatibility baseline and explain every intentional difference.

## What belongs in #893 now

Keep #893 focused on making the existing facade work with the merged EP list
contract. Its forwarding, upstream metadata preservation, validation/error
handling, and regression tests belong together. Retain the temporary classes
and page checker until the shared package replacement exists.

The optional small cleanup is shortening both route descriptions and
regenerating the EP YAML/TypeScript artifacts. Generic filter syntax belongs in
shared documentation. This is not required for the architectural refactor.
The module-level followup docstring is already an uncommitted local edit.

Do not put client-package extraction, public response expansion, new AO cluster
routes, RBAC, project associations, or a main OpenAPI pipeline conversion into
this PR. There is no need to add these changes before making #893 ready for
review. At research time GitHub already reports `isDraft: false`; no PR state
change was made.

## Delivery and remaining decisions

Deliver an additive EP/client contract release first, then a Syntara adoption
PR. EP's HTTP behavior stays compatible, so deployments need not be simultaneous.
Keep the current facade behavior until its replacement is available; do not
remove validation simply to reduce the diff.

The material product decision is whether AO retains its current response subset
or deliberately exposes EP's complete safe read contract. This plan defaults to
preservation pending that decision. Whether to fold the pre-existing separate
facade spec into the ordinary Syntara domain workflow is a separate tooling
decision; neither outcome prevents shared Pydantic contracts.
