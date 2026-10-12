# 修复混合后端发布后的持续推理服务

同一 LLM serving 集群逐步上线分页后端，并同时服务普通 completions、量化 logits 和结构化生成请求。上线验证出现几组不一致：容量预览正常而连续请求被拒；单独生成正常，夹在发布与取消之间却改变结果；一些流已经提交 token，客户端文本、资源占用和后续接纳仍对不上。请调查给定工程及原始导出，修复生产 `/v1/completions`，使这一发布期间的持续服务符合公开契约。

问题可能跨越多个组件。交付目标是同一部署下的生成正确性、可用容量及持续服务行为；数据准备、推理计算、派发和监控的结果必须彼此一致。定位哪些事实可信、哪些代码实际参与生产，以及如何修复，由你完成。

领域为 **ML / 训练推理评测与基础设施 / Inference**。工程基于 Apache-2.0 的 `vllm-project/production-stack`；环境从 `task.toml` 声明的公开 GitHub commit 拉取，来源见 `UPSTREAM.md`。CPU 后端和原始导出是合成复现材料，不声称 GPU 实测。

`docs/fabric-investigation.md` 提供材料入口与命令。以下七份文档**同时生效**，完整规定验收涉及的接口、格式、单位及行为边界：

- `docs/fabric-measurement.md`
- `docs/fabric-deployment.md`
- `docs/fabric-admission.md`
- `docs/fabric-protocol.md`
- `docs/fabric-speculation.md`
- `docs/fabric-constraints.md`
- `docs/fabric-paged-attention.md`

保留三种后端表示及线性/树状草稿的既有能力。`captures/paged-7/workload.json` 包含共享 gateway、租户、路径和发布状态的混合请求；其他三份 fabric/speculative/structured workload 是同一服务已有协议的回归输入。请验证整段持续状态，包括后续到达、发布、失败和取消期间的行为。历史文档继续约束旧 colocated/deadline CLI、deadline 服务及 RoundRobin/LoadAware 路由兼容性。

交付可执行修复源码，以及能从原始输入重新生成的五项基础 build JSON、存在语法输入时的 `grammar-profile.json`、`fabric-evaluation.json` 和 `metrics.prom`。公开 gateway、CLI、HTTP 及已声明控制入口须保留；内部模块、状态结构和数值内核可重构。JSON/诊断允许附加审计字段，身份可采用契约允许的 header/JSON 承载，浮点容差为 1e-5。

验收更换原始证据、版本可见性、数值布局、历史与并发时序，通过独立 transport 观察真实派发、HTTP、token/text、中间资源状态和指标，再与公开 replay 对账。所有通过条件均来自上述契约；公开测试只是有限示例。建议提供 `SERVING_DESIGN.md` 和新增回归证据，供审阅，不以文档字数或任意一项测试通过代替正确性。

环境为 `/workspace`，Python 3.12，依赖已安装，`PYTHONPATH=/workspace/src`。常用命令：

```sh
python -m serving_lab.fabric build --input captures/paged-7 --output out/paged --as-of 106100
python -m serving_lab.fabric replay --input captures/paged-7 --workload captures/paged-7/workload.json --output out/paged-replay
python -m pytest regression_tests -q
```

其他输入使用相同 build/replay 接口。以可复现的运行和持续行为完成交付；排查顺序、批量处理方式和工具调用次数由你决定。
