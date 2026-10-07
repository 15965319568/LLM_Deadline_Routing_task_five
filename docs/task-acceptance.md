# Task acceptance scope

The current task is the author deadline routing extension. Upstream README and
historical deployment guides do not add obligations such as starting Kubernetes,
downloading weights or fixing every optional upstream feature.

Required: the public deadline/monitoring contracts, actual completions routing,
calibration interpolation and cache namespace, observation applicability,
owner-scoped external work, atomic local reservations, cancellation/failed
connection/normal completion cleanup, first generated-token timing, feedback,
diagnostics, bounded metric labels, executable regression tests and replay.

Inputs use 3 CPU-backed endpoints, nonuniform token/decode grids, ASCII and
Unicode prompts, multiple owners, finite legal configuration values, stale/
future/duplicate/older/negative snapshots, valid and invalid cache namespaces,
cache lookup interruption, same-time requests, connection failure, cancellation
before first token and repeated terminal notifications. Feedback includes
repeated span identities. Values, IDs, lengths, intervals, admission deadlines
and the order/number of ordinary rows may vary within the documented contracts.

Compatibility is checked for the upstream round-robin endpoint ordering and
loadaware cache-benefit clipping/score. The author additionally runs upstream
unit tests, but arbitrary historical modes do not automatically become hidden
grading requirements.

ROUTING_DESIGN.md must be nonempty and truthfully explain diagnosis, chosen
repair, actual validation and limitations. regression_tests/test_*.py must
contain at least one pytest-discoverable test; the suite must run successfully
without skipped tests. Report length and particular headings are not scored.
These checks cannot alone prove report truth or regression completeness.

Three private groups examine deliverables, compatibility and the live gateway.
The gateway group contains three changed workloads and the replay command.
All groups must pass for reward=1; otherwise reward=0. Each workload stops at its
first failure while the other workloads still run. A complete verifier report
means the grading process completed, not every later assertion was reached.
Float node values have the monitoring contract tolerance. Microsecond event
times/route choices/status codes and aggregate counts remain exact.

Official private Python/shell scoring is a small bounded harness. All private
orchestration and assertions are in tests/grader.py and tests/test.sh. JSON is
ordinary input/expected data with no executable DSL. The publicly provided
exercise is a candidate deliverable independently checked against expectations;
the grader also drives the live HTTP path itself and does not trust its output
as a scoring oracle. No candidate-private table, filename or lock is graded.

Not separately covered: arbitrary topology restarts, distributed gateway
coordination, retry chains, model accuracy, actual GPU latency or throughput,
full chat/multimodal protocols, every possible malformed SSE stream, arbitrary
mid-decode client failure, long-term memory bounds, all HTTP status families,
healthy/draining changes in every interleaving, or each individual Prometheus
family's exact value. TTFT count/sum, correction, reserved work and decode gauges
are directly compared; routing/age/drift/errors are also covered by diagnostics
and summaries, without claiming an independent exact check of every metric.

No fixed tool count, investigation order, required wall time or mandated
implementation. Difficulty must be measured by new independent model runs.
