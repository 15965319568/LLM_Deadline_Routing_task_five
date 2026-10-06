# Source provenance

Upstream: https://github.com/vllm-project/production-stack

Source commit: 014d070e6f7611978d321bdb05cbe9a934b614e7

The complete source snapshot is retained, with Apache-2.0 LICENSE and upstream
copyrights. The author adds the deadline package, embedding bootstrap, CPU
exercise, synthetic workloads and task contracts. Limited lifecycle hooks are
added to src/vllm_router/services/request_service/request.py so the extension
runs on the actual upstream forwarder. No upstream issue or patch is claimed
as the source of this task's deliberately incomplete pilot semantics.

The initial pilot is an authored implementation for repair. Workloads and
backend service spans are synthetic; they are not customer logs or evidence of
a real upstream outage. Private expected data, reference solutions and author
validation tools are not part of this public starter.
