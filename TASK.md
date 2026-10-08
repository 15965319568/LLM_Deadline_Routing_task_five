# 修复滚动部署下的 LLM 分离式推理与 KV 兼容性

推理集群把一个逻辑模型的 prefill、KV transfer 和 decode 分到不同服务。一次
混合版本上线后，容量报表看起来足够，在线却出现请求被过度拒绝、KV 不能被 decode
解释、流已返回文本但账本不释放、画像更新后反馈漂移等现象。部署目录、运行时装载
清单与 rollout 导出来自不同系统，文件到达顺序不代表生效顺序；原始测量和缓存页
也没有预先清洗。旧 colocated notebook 和 pilot 仍留在工程中。

请修复实际生产入口，使同一请求从证据重建、版本选择、组合接纳、三阶段转发到
终态监控保持一致。尤其要使滚动部署期间的在途推理、旧 KV 页、阶段反馈和新请求
分别使用正确的部署事实。模型别名相同并不意味着 tokenizer、量化格式、adapter
和 KV schema 可互换；同一资源换代而成本未变，也会改变运行时状态的含义。

任务属于 **ML / 训练推理评测与基础设施 / Inference**，细分方向为推理实现与
LLM serving 栈、服务基础设施与生产监控/漂移。使用 Apache-2.0 的
vllm-project/production-stack 生产路由入口；上游与许可证见 UPSTREAM.md。
原始导出和 CPU 后端是独立编写的合成故障材料，验证推理协议与状态守恒，
不将仿真成本宣称为 GPU 实测。

以下四份文档同时生效，包含所有自动验收所要求的接口、字段、单位和边界：

- docs/fabric-measurement.md：生产时钟、原始测量、分页证据、画像与漂移。
- docs/fabric-deployment.md：registry/runtime/rollout 裁决、模型兼容性、部署换代。
- docs/fabric-admission.md：组合选路、资源/租户账本、健康、亲和、反馈与监控。
- docs/fabric-protocol.md：真实三阶段协议、ACK、SSE、公开控制入口和 replay。

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
python -m pytest regression_tests -q
```

不限制批量操作，不规定工具调用次数，也不要求人为等待。
