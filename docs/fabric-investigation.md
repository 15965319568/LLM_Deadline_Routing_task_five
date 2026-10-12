# 持续推理发布的材料入口

任务入口为 `TASK.md`。工作对象是一个正在迁移后端表示的推理服务。旧客户端、logits 后端与分页后端在同一服务中共存；部署名称保持不变时，装载事实、语法版本和在途请求仍可能处于不同时间点。

生产入口是 `/v1/completions`。本次公开 `captures/paged-7/workload.json` 在一个 gateway 生命周期中安排分页请求与 logits/结构化请求、滚动发布、失败、取消及后续到达。所有请求使用既有接口和契约，共享对应资源、租户与路径状态。`captures/fabric-7`、`captures/speculative-7`、`captures/structured-7` 保留此前协议的独立回归输入，用于确认同一修复在不同后端表示下仍可运行。

每个输入目录的 `fabric.json` 指向测量、部署、语法和解码资产。清单中的 CSV、JSONL、gzip 记录来自不同导出源，保留原始次序和可见时间。`workload.json` 描述外部请求、控制事件和后端返回时点；运行时只接收公开接口送达的信息。notebook、pilot、启动配置与原始导出各自的用途，以对应契约定义为准。材料没有提供故障位置、修复清单或排查步骤。

七份同时生效的规范是 `fabric-measurement.md`、`fabric-deployment.md`、`fabric-admission.md`、`fabric-protocol.md`、`fabric-speculation.md`、`fabric-constraints.md`、`fabric-paged-attention.md`。它们规定外部语义，不限制内部实现。数值公式用于定义协议结果；数据裁决、模块设计、算法实现与故障归因是待完成工作。

完成条件包括不同 cutoff 的证据重建、真实推理生成、连续 checkpoint 以及 metrics/replay 一致性。检查应覆盖流存活期间和后续请求，终态结果只是其中一部分。所有原始文件和公开测试均可本地访问。

```sh
python -m serving_lab.fabric build --input captures/paged-7 --output out/paged --as-of 106100
python -m serving_lab.fabric replay --input captures/paged-7 --workload captures/paged-7/workload.json --output out/paged-replay
python -m serving_lab.fabric replay --input captures/structured-7 --workload captures/structured-7/workload.json --output out/structured-replay
python -m serving_lab.fabric replay --input captures/speculative-7 --workload captures/speculative-7/workload.json --output out/speculative-replay
python -m serving_lab.fabric replay --input captures/fabric-7 --workload captures/fabric-7/workload.json --output out/fabric-replay
python -m serving_lab build --input captures/capture-5 --output out/colocated --as-of 100000
python -m pytest regression_tests -q
```

上游路由代码沿用许可证及来源记录。新增 CPU 复现输入为合成数据，用于推理数值、服务协议和持续状态验证，不是线上客户日志或 GPU 性能结论。
