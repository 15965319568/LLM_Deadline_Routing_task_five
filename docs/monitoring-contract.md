# Monitoring and exercise outputs 1.0

GET /deadline/diagnostics (also inspect()) returns `nodes` keyed by endpoint_id
and `decisions` (list order is not significant). Each node has reserved_us, decoding, correction, drift
and age_us (-1 if no usable snapshot). reserved_us/correction are numeric with
absolute tolerance 0.00001; all other node values are exact. Decisions contain
request_id, arrival_us, status, endpoint_id and predicted_first_us. The latter
two are null for rejection. Status here is admission status: a backend failure
does not rewrite an earlier 200 admission to pretend the request was rejected.
Arrival order is the order offers enter the async gateway; same-time offers in
the supplied exercise are submitted in the listed order.

GET /metrics exposes a private CollectorRegistry for this gateway, with only
endpoint, model, result, state and standard histogram le labels. No request_id,
user prompt or token sequence becomes a metric label. Model labels for this
exercise belong to its finite configured input set. Required families:

| Name | Meaning |
|---|---|
| deadline_routes_total | Admission outcomes by model/result HTTP code |
| deadline_ttft_seconds | Histogram for first generated token, measured from arrival |
| deadline_prediction_error_seconds | Latest actual TTFT minus admission-time predicted TTFT per endpoint |
| deadline_reserved_prefill_us | Outstanding local prefill work |
| deadline_local_decodes | Active local decode count |
| deadline_correction | Current service multiplier |
| deadline_snapshot_age_us | Age of usable sample, otherwise -1 |
| deadline_drift | One-hot status among insufficient_data/normal/slow/fast |

summary(start_us,end_us) covers requests arriving in the half-open window.
offered includes rejections; accepted includes eventual failures; rejected is
the complement. completed requires successful termination after a first token.
deadline_missed counts terminal accepted requests with no first token or a first
token later than deadline. A post-first-token failure need not be a deadline
miss but never contributes to goodput. goodput_per_s is successful requests with
on-time first token divided by window duration in seconds, rounded to 6 decimal
places. TTFT p95 uses nearest rank ceil(.95*n) over requests with a first token,
even if later failed; empty population returns null. Pending requests are not
silently counted as completed. The exercise drains all work before summary.

The supplied command `python -m routing_exercise --scenario INPUT --output OUT`
replays ordinary workload data through HTTP/ASGI and the actual upstream request
forwarder. It creates routing-evaluation.json with scenario, summary and phases,
and metrics.prom. Input rounds contain snapshots, cache declarations, request
bodies, backend diagnostic spans and explicit barriers at arrival, first-token
and completion times. These controlled observations are protocol tests, not a
performance simulation or measured GPU benchmark. Different policy decisions
affect acceptance, dispatch, reservations and summary population; no GPU
speedup or fixed performance improvement threshold is asserted.

The summary fields above are mandatory and checked on changed inputs. Phase
diagnostics aid reproduction; their internal reporting layout is not separately
fixed by scoring. JSON field order, insignificant whitespace and UTF-8 BOM are
accepted. Human findings belong in ROUTING_DESIGN.md, not hardcoded output JSON.
