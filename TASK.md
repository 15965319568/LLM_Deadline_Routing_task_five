# 修复量化 KV 跨窗口复用中的推理与持有偏差

同一批分块 KV 在 worker 间搬运并被连续请求复用后，出现以下故障：某些重排后的记录被拒绝，另一些记录能完成当前窗口却在下一窗口输出不同 token；改变接受前缀或读回执时刻，还会改变后续请求的可用容量。旧读者与新读者有时对同一来源表现不同。请从 captures 和实际生效源码调查这些现象，修复 `/v1/completions` 的量化分页路径及同一批 KV 的 `/fabric/kv-reads` 生命周期。

方向为 **ML / 训练推理评测与基础设施 / Inference**。这是 production-stack 基础上的合成 CPU 推理故障复现，不是上游漏洞或 GPU 性能测试。环境从公开仓库真实拉取固定 commit，来源见 UPSTREAM.md。

全部生效要求仅为以下两份文档，包括全部小节：

- `docs/paged-kv-contract.md`：分页数值、输入记录、量化视图与持续证据、共享及发布规则。
- `docs/paged-service-boundary.md`：给定依赖、公开入口、事件顺序、资源与验收字段。

交付可执行源码。原因定位、内部设计和修复方式由你确定。captures 中的分页会话与 regression_tests 是可运行调查入口；输入记录并不保证按目录、数据或版本顺序到达。验收通过独立 transport 驱动真实 HTTP 推理、连续窗口、读取回执和后续接纳，输入会改变数值、证据选择、视图及交错组合。

画像加载、通用接纳、采样数学、文本与 SSE 已提供基础实现；其与分页计算、发布和物理持有之间的接合属于修复范围。不另行验收画像编译、deployment、grammar、完整 metrics/replay 或旧 CLI。其他资料不增加交付义务。允许重构和额外审计字段；只比较契约声明的公开行为，浮点容差 1e-5。

环境为 `/workspace`、Python 3.12，依赖已安装，`PYTHONPATH=/workspace/src`。无需先 build，可运行：

```sh
python -m pytest regression_tests -q
```

公开回归覆盖部分合法与错误输入；完整验收覆盖两份契约允许的其他连续组合。不要求固定源码文件数、测试数、报告或工具调用次数。
