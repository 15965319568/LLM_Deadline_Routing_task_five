# 修复分页量化注意力与结构化推理的跨窗口一致性

分离式 LLM serving 集群在混合版本上线后出现过度拒绝、KV 不兼容、token 偏离 target 分布、停止串透出和资源不释放。请修复 `/v1/completions` 生产入口，将原始证据重建、部署选择、prefill—KV transfer—decode、量化/结构化 speculative 生成与监控连成一致的执行过程。线性及树状草稿均须支持；滚动发布、取消和跨窗口生成期间的持续状态也须正确，不能只核对终态。

领域为 **ML / 训练推理评测与基础设施 / Inference**，涉及推理实现、LLM serving 栈及生产监控/漂移。工程使用 Apache-2.0 的 vllm-project/production-stack，来源见 `UPSTREAM.md`。原始导出与 CPU 后端是合成故障材料，不代表 GPU 实测。

分页后端上线后，健康预览正常但生成随导出顺序改变；旧流未结束时的后续接纳也不稳定。调查记录见 `docs/paged-incident.md`，完整背景与命令见 `docs/fabric-investigation.md`。以下七份文档**同时生效**，规定自动验收所需的接口、字段、单位和边界：

- `docs/fabric-measurement.md`：原始测量、生产时钟、页证据、画像与漂移。
- `docs/fabric-deployment.md`：registry/runtime/rollout 裁决、部署换代与模型兼容性。
- `docs/fabric-admission.md`：组合选路、资源/租户账本、缓存、健康、反馈与监控。
- `docs/fabric-protocol.md`：三阶段转发、ACK、SSE、公开控制入口及 replay。
- `docs/fabric-speculation.md`：量化分片、target/draft 采样、草稿提交/回滚与生成监控。
- `docs/fabric-constraints.md`：多格式语法证据、修订父链、前缀分布、停止串与树状草稿。
- `docs/fabric-paged-attention.md`：`paged-gqa` 张量协议、分页 KV、注意力计算、工作区与提交语义。

历史文档只约束旧 colocated/deadline 入口，不覆盖新 fabric 语义；旧 CLI、deadline 服务及普通 RoundRobin/LoadAware 路由仍须可用。

交付可执行修复源码，以及可从原始输入重新生成的五项基础 build JSON、结构化输入的 `grammar-profile.json`、`fabric-evaluation.json` 和 `metrics.prom`。允许重构内部模块；保留公开 gateway、CLI、HTTP 及文档声明的控制接口。诊断和 JSON 允许附加审计字段，线路身份按协议允许的 header/JSON 表示承载。

验收会更换原始导出、成本、可见性、语法图、分片、历史、并发阶段及取消时点；独立观察真实派发、HTTP 状态、token/text、账本和指标，再与公开 replay 对账。SSE 分片或合包须保持语义；不得只改 replay 或写死产物。公开测试是有限示例：完成前应重建并核对 `captures/fabric-7`、`captures/speculative-7`、`captures/structured-7`、`captures/paged-7` 四份 workload 的连续 checkpoints、生成结果和 metrics。

建议附带 `SERVING_DESIGN.md` 说明证据取舍、调用路径与守恒，并在 `regression_tests/` 保留真实回归。这些建议用于审阅，不以字数、关键词或任意一项测试通过代替行为验收。私有测试不提供给 agent；不要求固定内部 helper、排查顺序、工具调用次数或人为等待。

环境：`/workspace`，Python 3.12，依赖已安装，`PYTHONPATH=/workspace/src`。常用入口：

```sh
python -m serving_lab.fabric build --input captures/structured-7 --output out/structured --as-of 106100
python -m serving_lab.fabric replay --input captures/structured-7 --workload captures/structured-7/workload.json --output out/structured-replay
python -m pytest regression_tests -q
```

公开回归包括 `test_serving_lifecycle.py`、`test_contract_boundaries.py` 和 `test_constraint_serving.py`，覆盖中途接纳/释放、输入与 SSE 边界、语法生成及逐帧状态。分页输入使用相同 build/replay 命令，将 input 改为 `captures/paged-7`，并运行 `test_paged_attention.py`。其它命令见 `docs/fabric-investigation.md`；所有通过条件以七份生效契约为准。
