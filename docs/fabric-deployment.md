# Bundle prepare / commit fence

The six profile and policy artifacts form one immutable `profile-bundle/v1`.
`bundle-manifest.json` contains the fixed `artifact_order` and a `bundle_id`
equal to the SHA-256 of the canonical `{schema,as_of_us,artifacts}` object.
The gateway must verify that identity before using a directory.

Workloads may contain `deployments` in addition to ordinary request events. A
deployment has `at_us`, `operation` (`prepare`, `commit`, or `abort`) and a
`build` name. `prepare` validates the complete bundle and records one pending
bundle without changing admission, resource epochs, policies, or in-flight
reservations. At most one bundle may be pending. `commit` succeeds only for
the pending `bundle_id` and its recorded parent (an optional `parent_bundle`
field is a consistency check); it then switches profiles and policies as one
critical-section operation. Existing requests retain their admission-time
baseline, route generation and policy revision. `abort` discards the pending
bundle and leaves live state unchanged.

Repeated prepare, commit without prepare, wrong parent, unknown operation and
abort for another bundle are audit-only outcomes. They must not mutate the
active bundle. A successful prepare, commit and abort append `reload` journal
events with an `action` payload; audit counters remain low cardinality. The
legacy `reload(profile_dir)` entry remains available as a prepare followed by
commit for compatibility.

Diagnostics expose `bundle_id`, monotonic `bundle_generation` and a redacted
`pending_bundle` (`bundle_id`, `parent_bundle`, `as_of_us`). `/metrics` exports
only the unlabeled `fabric_bundle_generation` gauge. Live replay and ASGI
replay must observe the same active bundle at every checkpoint.
