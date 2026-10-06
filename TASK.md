# 修复策略热更新下分离式 LLM Serving 的证据仲裁与请求账本

同一个模型被拆为 prefill、KV 传输和 decode 服务。现有试点沿用了 colocated
容量 notebook 和独立端点选择：离线汇总能生成，在线也能返回流，但在不同 KV
布局、缓存页、迟到测量和重叠请求下，接纳结果、实际派发与资源占用不一致。
测量更新期间，晚到阶段反馈又可能污染新画像。

另外，试点接入了多租户配额、会话复用和动态健康状态：全局资源有空闲并不代表租户可以接纳，decode 的完整页和三阶段 work 预留必须
在租户和资源两个账本中同时可见；同一会话的缓存命名空间和路径亲和也不能被普通缓存页或排空节点绕过。
生产入口还要处理租户滚动 start 窗口、按优先级加权的 work 账单、带世代校验的 KV
页血缘，以及要求完整 SSE DONE 终止标记的流状态。

V7 的生效协议把一次请求视为一个跨阶段 reservation transaction。离线证据
必须先通过 `phase/v7` manifest：每个原始行有 schema、来源文件、逐行签名，
manifest 还绑定各个 CSV/JSONL/gzip 文件的 checksum；格式转换不能改变行的
身份。在线路径由带 generation 的 route alias 管理，拓扑事件可能在 reload、
缓存替换和请求已经进入 prefill 后到达；排空的 route 不能接新请求，但在途
事务仍必须按旧代次完成或回滚。

prefill、KV transfer、decode 的 ack 必须回显 request、transaction、phase
token、route generation 和布局；任何一项不匹配都要停止后续派发，并按阶段
释放 reservation。阶段反馈还必须绑定 trace、phase、resource epoch 和
sample signature，迟到、重复或跨 reload 的反馈只能进入审计而不能污染新画像。
KV lease 除了 producer/generation/page hash 外还受撤销记录约束；租户的 burst
credit、失败惩罚、start window、slot/page/work 三本账要在取消、失败、重复
终态和同刻到达时保持守恒。诊断可以包含审计计数，但 metrics 标签必须保持
低基数。

请完成该工程迁移，使保留原貌的测量与证据生成可靠的阶段画像，让实际
`/v1/completions` 入口使用可兼容的 prefill—传输—decode 组合，并正确处理
持续流量中的全额预留、阶段释放、失败、取消、画像更新、动态健康/排空、会话亲和、租户 work 预算和监控。

本题属于推理实现、LLM serving 栈与生产监控/漂移。基于 Apache-2.0 的
vllm-project/production-stack，保留上游普通路由和真实 HTTP/ASGI 入口。
新增服务协议、数据及故障是独立编写的合成工程材料；CPU 回放验证协议与
状态，不代表实测 GPU 性能。上游 commit 和许可证见 `UPSTREAM.md`。

控制面导出还包含草稿、shadow 副本、未来记录、同 revision 冲突和父链断裂。
迟到反证可能使已应用策略回退，同时旧请求跨越 reload 继续使用固定罚金和预留。
测量画像与租户策略必须作为同一 bundle 原子发布，信用退款、补充、裁剪、失败
债务要与阶段释放和新接纳共同对账。导出顺序、最新 revision 和单次 HTTP 成功
均不能证明整个迁移正确。

本次生效要求是 `docs/fabric-measurement.md`、`docs/fabric-admission.md`、
`docs/fabric-control.md` 和 `docs/fabric-protocol.md`，以及 `captures/fabric-7/phase/manifest.json` 与
其 workload 中的 topology/transaction 事件。此前 colocated 测量与 deadline 入口保持可用；
notebook、旧试点和历史契约只解释旧路径，不覆盖分离式要求。

交付：可执行修复源码；由原始输入重新生成的阶段样本账、画像、漂移和回放
产物、租户策略审计账与原子发布 manifest；非空 `SERVING_DESIGN.md`，解释证据
取舍、生效路径、迟到冲突重算和资源/信用守恒；
`regression_tests/` 下真实运行且通过的回归测试。验收更换数值、覆盖范围、
导出顺序、缓存证据及事件重叠，直接观察真实后端调用及 HTTP 结果。
允许重构，不限定私有模块名或内部类结构。不能用固定输出代替实现。

环境：`/workspace`，Python 3.12，依赖已安装，`PYTHONPATH=/workspace/src`。
保留普通 RoundRobin/LoadAware 行为。公开的 gateway 和 CLI 是兼容入口。

```sh
python -m serving_lab.fabric build --input captures/fabric-7 --output out/fabric --as-of 100000
python -m serving_lab.fabric replay --input captures/fabric-7 --workload captures/fabric-7/workload.json --output out/replay
python -m serving_lab build --input captures/capture-5 --output out/colocated --as-of 100000
python -m pytest regression_tests -q
```

公开回放用于调查；请核对其输出与真实转发一致。任务不限制批量操作，
不指定工具调用次数，不要求人为等待。
