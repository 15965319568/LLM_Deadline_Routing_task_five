# Source provenance

Upstream: https://github.com/vllm-project/production-stack
Upstream commit: 014d070e6f7611978d321bdb05cbe9a934b614e7
License: Apache-2.0; original LICENSE and copyrights retained.

This authored CPU reproduction uses the production-stack serving entrypoint.
It is not a reported upstream defect, a customer trace, or a GPU benchmark.
The V6.9 exercise narrows repair responsibility to paged inference and KV
ownership. General serving, profile/deployment loading, sampling and SSE are
provided working dependencies. Synthetic profiles are supplied inputs.
The only active exercise contracts are docs/paged-kv-contract.md and
docs/paged-service-boundary.md. Prior exercise contracts and independent
regression workloads have been removed from this starter.

The public starter contains no private scenarios, oracle implementation or
author validation tools. The task environment fetches its declared immutable
GitHub commit over HTTPS rather than copying a local source bundle.
