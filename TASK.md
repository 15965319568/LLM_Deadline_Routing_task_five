# 修复量化推理的持续状态、Speculative KV 回滚与滚动部署一致性

推理集群把一个逻辑模型的 prefill、KV transfer 和 decode 分到不同服务。一次
混合版本上线后，容量报表看起来足够，在线却出现请求被过度拒绝、KV 不能被 decode
解释、流已返回文本但账本不释放、画像更新后反馈漂移等现象。部署目录、运行时装载
清单与 rollout 导出来自不同系统，文件到达顺序不代表生效顺序；原始测量和缓存页
也没有预先清洗。旧 colocated notebook 和 pilot 仍留在工程中。

新接入的 speculative decode 在混合量化部署下还出现了另一条故障链：分片各自看似正常，
最终 token 却偏离 target 分布；草稿被拒绝后，KV 页和输出位置不一致；一个字符跨
两个提交窗口时，TTFT 与流终态又产生分歧。在线服务同时承接普通 SSE 和新数值协议。
需要用原始证据和可运行场景定位问题，修复量化 logits、采样分布、草稿提交及其与
资源账本和部署快照的关系。题目没有预先列出需要修改的模块和根因。

还有一种生产现象：单请求生成文本正常、终态页数也归零，但长流尚未结束时的
后续接纳和滚动发布仍不正确。同一时刻的物理阶段占用、租户在途账单、草稿窗口
和实际已提交文本具有不同生命周期；不能用一次最终快照代替全过程的验证。

请修复实际生产入口，使同一请求从证据重建、版本选择、组合接纳、三阶段转发到
终态监控保持一致。尤其要使滚动部署期间的在途推理、旧 KV 页、阶段反馈和新请求
分别使用正确的部署事实。模型别名相同并不意味着 tokenizer、量化格式、adapter
和 KV schema 可互换；同一资源换代而成本未变，也会改变运行时状态的含义。

任务属于 **ML / 训练推理评测与基础设施 / Inference**，细分方向为推理实现与
LLM serving 栈、服务基础设施与生产监控/漂移。使用 Apache-2.0 的
vllm-project/production-stack 生产路由入口；上游与许可证见 UPSTREAM.md。
原始导出和 CPU 后端是独立编写的合成故障材料，验证推理协议与状态守恒，
不将仿真成本宣称为 GPU 实测。

以下五份文档同时生效，包含所有自动验收所要求的接口、字段、单位和边界：

- docs/fabric-measurement.md：生产时钟、原始测量、分页证据、画像与漂移。
- docs/fabric-deployment.md：registry/runtime/rollout 裁决、模型兼容性、部署换代。
- docs/fabric-admission.md：组合选路、资源/租户账本、健康、亲和、反馈与监控。
- docs/fabric-protocol.md：真实三阶段协议、ACK、SSE、公开控制入口和 replay。
- docs/fabric-speculation.md：量化张量分片、目标/草稿采样分布、提交与回滚、生成 token 和监控。

历史文档只约束旧 colocated/deadline 入口，不覆盖新 fabric 语义。旧 CLI、
deadline 服务及普通 RoundRobin/LoadAware 路由仍须工作。

交付可执行修复源码，以及可从原始输入重新生成的五项 build JSON 和
fabric-evaluation.json / metrics.prom。允许重构任何内部模块；只要求保留公开
gateway、CLI、HTTP 和文档声明的控制接口。自动验收更换原始导出、成本、可见性、
并发阶段与取消时点，独立观察真实派发、HTTP 状态、账本和指标，再与 replay 对账。
诊断和 JSON 允许附加审计字段；不得通过固定结果或只修改 replay 通过。

建议附带 SERVING_DESIGN.md 解释证据取舍、调用路径与守恒，并在 regression_tests/
留下真实回归测试。这些说明和自建测试用于审阅，不以字数、关键词或任意一个测试
通过代替行为验收。仓库中的公开测试可用于定位问题，私有测试不会提供给 agent。

环境为 /workspace，Python 3.12，依赖已安装，PYTHONPATH=/workspace/src。

```sh
python -m serving_lab.fabric build --input captures/fabric-7 --output out/fabric --as-of 100000
python -m serving_lab.fabric build --input captures/fabric-7 --output out/rotated --as-of 104100
python -m serving_lab.fabric replay --input captures/fabric-7 --workload captures/fabric-7/workload.json --output out/replay
python -m serving_lab build --input captures/capture-5 --output out/colocated --as-of 100000
python -m serving_lab.fabric replay --input captures/speculative-7 --workload captures/speculative-7/workload.json --output out/speculative
python -m pytest regression_tests -q
python -m pytest regression_tests/test_serving_lifecycle.py -q
```

公开测试包含已知证据边界、真实 HTTP 数值回归与持续状态检查。test_serving_lifecycle.py
通过独立后端屏障检查请求中途的诊断、指标及下一笔接纳，包含 success/error/cancel、
成本 reload、UTF-8 半字符跨窗口、scratch 容量失败和乱序原始记录分类。
这些测试是可读的有限示例，全部通过仍不意味着所有输入都已覆盖。完成前应核对两份
公开 workload 的连续 checkpoints，并将生成结果、实时派发与 metrics 按生效契约对账；
保留所发现边界的回归证据。无需沿用测试的内部组织方式或新增固定命名的 helper。
不限制批量操作，不规定工具调用次数，也不要求人为等待。
