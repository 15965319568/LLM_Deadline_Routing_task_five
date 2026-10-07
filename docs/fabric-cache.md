# Versioned KV cache publication

The KV lease image is a control-plane object separate from the profile bundle.
It is published with an integer `generation`; the gateway starts at generation
`0` with no active leases. A cache workload event contains `operation`,
`generation`, `parent_generation`, `leases`, and `revocations`.

`activate` with generation `1` and parent `0` bootstraps the first image. Later
images must be staged before activation:

1. `stage` accepts exactly `generation = active + 1`,
   `parent_generation = active`, and only when no image is pending. It stores
   the complete lease and revocation image without changing live lookups.
2. `activate` accepts only the matching pending generation and parent. It
   atomically swaps both leases and revocations and clears the pending image.
3. `abort` drops the matching pending image. `revoke` is an in-place delta
   whose generation and parent both equal the active generation; duplicate
   revocation rows are ignored.

Wrong parents, skipped generations, duplicate stages, activation without a
stage, an abort for another generation, malformed rows, and unknown operations
are audit-only outcomes. They never change active leases or generation. A
request records the active `cache_generation` at admission and keeps its
cached prefix after a later activation. Diagnostics expose the active integer
and a redacted `pending_cache` (`generation`, `parent_generation`,
`lease_count`, `revocation_count`). `/metrics` exports the unlabeled
`fabric_cache_generation` gauge. Successful stage/activate/abort/revoke
operations append a low-cardinality `cache` journal event; invalid operations
only increment `cache:<reason>`.

The replay driver must deliver cache events in timestamp and source order and
must produce the same cache generation, request snapshots, journal head, and
wire calls as a live ASGI run.
