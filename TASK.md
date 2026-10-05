# 异构 LLM 推理集群的首 Token 时限路由与负载漂移治理

你负责基于 vLLM Production Stack 的推理网关。不同性能的副本接入长文和
热门前缀流量后，试运行的 deadline 路由出现拒绝、容量占用及监控偏差。
请根据产品契约和随附演练材料调查，修复实际请求链路，并验证兼容行为。

正式要求见 docs/deadline-routing-contract.md、docs/monitoring-contract.md；
输入和评分边界见 docs/task-acceptance.md。上游已有路由代码与部署文档保留，
任务新增的 CPU 试验层及材料来源见 UPSTREAM.md。

交付修复源码、regression_tests/ 中实际通过且无跳过的新增 pytest 回归、
非空且真实的 ROUTING_DESIGN.md，并使随附演练命令能在新输出目录生成
routing-evaluation.json 与 metrics.prom。报告说明依据、实现取舍、验证及局限。

验收会沿真实 completions 转发入口检查改变参数的工作负载、并发事件与监控，
不只检查某个离线函数或手填结果。可自由重构内部实现、批量调查和选择工具。

环境为 Linux/Python 3.12，CPU 依赖预装、PYTHONPATH=/workspace/src。
不需要 GPU、权重、Kubernetes 或付费 API。

```bash
python -m pytest pilot_tests -q
python -m pytest regression_tests -q
python -m routing_exercise --scenario workloads/mixed-load.json --output /tmp/deadline-replay
```

regression_tests 起初为空，应提交自己的回归。公开演练数据没有标准答案表；
应依据正式规则解释观察，并用改变后的输入验证实现。
