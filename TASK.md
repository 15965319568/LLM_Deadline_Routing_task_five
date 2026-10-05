# 从原始压测证据重建 LLM Serving 成本画像并修复持续流量接纳

推理网关的离线画像和线上时限判断发生偏离。最新容量汇总看起来正常，
但混合长度、缓存和并发解码的流量下，网关会使用不适用的成本依据；画像
重新生成后，在途请求和晚到反馈还可能让后续接纳失真。

请修复这个工程：从保留原貌的压测导出、运行清单、服务 trace 与缓存证明
重建可用画像，让真实 completions 入口使用这些画像，并正确处理持续流量
中的接纳、画像替换、容量释放、反馈与监控。必要的证据不足必须明确表现为
不可用，不能用零、其他设备的样本或旧汇总补齐。

本题基于 Apache-2.0 的 vllm-project/production-stack，保留真实路由与转发
链。数据和扩展均为独立编写的合成工程材料；CPU 时钟回放只验证协议和状态，
不声称复现 GPU 性能。上游来源见 `UPSTREAM.md`。

当前生效要求见 `docs/serving-calibration-contract.md`、
`docs/export-formats.md` 和 `docs/continuous-replay.md`。
`docs/deadline-routing-contract.md`、旧 `routing_exercise` 与 notebook summary
保留为历史资料。V2 的范围、覆盖策略及反馈分母以本次契约为准。

交付内容：修复后的源码；可以从输入重新生成的样本账、画像、漂移与回放产物；
非空 `SERVING_DESIGN.md`，说明数据取舍、证据不足、代码生效路径和生命周期；
`regression_tests/` 下至少一个真实运行且通过的回归测试。不能依赖已生成产物
代替实现，验收会使用不同的原始输入、数值、记录顺序和事件重叠。

工作目录 `/workspace`，Python 3.12，依赖已经安装，`PYTHONPATH=/workspace/src`。
保持普通 RoundRobin/LoadAware 路由行为与上游接口兼容。允许重构新增模块和
增删源码，不固定私有类名或修补文件范围。

```sh
python -m serving_lab build --input captures/capture-5 --output out/profiles --as-of 100000
python -m serving_lab replay --input captures/capture-5 --workload captures/capture-5/workload.json --output out/replay
python -m pytest regression_tests -q
```

公开回放是调查工具；需要核对它与真实 HTTP/ASGI 转发行为的一致性。测试不要求
指定工具使用次数，也不以人为等待或生成额外文件数量计难度。
