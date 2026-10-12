# 修复滚动发布下分页推理的证据与运行状态偏差

分离式 LLM serving 集群在滚动上线分页后端后出现一组相互影响的故障。请调查原始采集数据与生产 `/v1/completions` 的执行，找出并修复多处问题，使离线画像、部署选择、实际生成、持续接纳和监控回放相互一致。

目前掌握的现象如下，具体根因和影响范围需要结合数据与代码确定：

- 测量导出包含重试、迟到和冲突记录，部分资源的样本支持数与画像成本异常；修改画像后，原来可接纳的请求又可能被拒绝。
- 发布记录、运行时就绪记录和注册表看似都有效，但某些时点实际派发的部署与其 KV、投影资产不一致；回滚后旧流仍在生成。
- 单个分片的数值范围正常，合并后 token 却偏离；改变页和 query 的导出次序，也可能改变结果。
- 第二个窗口复用旧 KV 后，重传与差分页使占用记录相互矛盾；部分流已输出正确前缀，后续失败或取消却影响了新请求。
- 新旧流共存时，服务耗时反馈、接纳结果和监控状态互相解释不通；只核对最后一帧或最终页数不能稳定复现。

工程已有各部件的实现，不需要新增一套服务。问题分布在数据处理、数值推理及在线状态的多处路径；允许重构内部实现，保留公开接口和正常行为。

领域为 **ML / 训练推理评测与基础设施 / Inference**。代码基于 Apache-2.0 的 production-stack，来源见 `UPSTREAM.md`；输入和 CPU 后端均为合成故障材料，不是 GPU 性能实测。

从 `docs/serving-incident.md` 和 `captures/paged-7/workload.json` 调查。以下七份文档同时生效：`fabric-measurement.md`、`fabric-deployment.md`、`fabric-admission.md`、`fabric-protocol.md`、`fabric-speculation.md`、`fabric-constraints.md`、`fabric-paged-attention.md`（均在 `docs/`）。它们规定接口、字段、单位、数值及错误边界，不指定故障位置或排查顺序；历史 colocated/deadline 文档只约束旧入口。

这些问题共享实际依赖：采集证据决定部署画像和接纳成本；选中的部署决定推理资产及页解释；生成窗口改变缓存与共享占用；旧流的反馈又参与后续接纳。单个部件局部正确不代表这条执行链正确，修复需要由可观察证据验证。

私有验收会变化输入值和导出顺序，通过独立 transport 驱动真实 HTTP/ASGI 请求，观察 token/text、阶段派发、连续页账本、竞争请求的状态和 metrics，再与公开 replay 对账。五组分页场景包含“在途切换部署—跨窗口差分页—竞争接纳—取消后恢复”的连续事件。仅修 replay、预制报告或最终清零不能替代实际执行。诊断允许附加审计字段；身份可使用协议允许的 header/JSON 表示，内部 helper 名与模块布局不作限制。

交付修复源码及可重建的 build/replay 产物。`SERVING_DESIGN.md` 和自建回归可辅助说明，但不以文档字数或任意一条测试通过代替行为验收。环境为 `/workspace`、Python 3.12，依赖已安装，`PYTHONPATH=/workspace/src`：

```sh
python -m serving_lab.fabric build --input captures/paged-7 --output out/paged --as-of 106100
python -m serving_lab.fabric replay --input captures/paged-7 --workload captures/paged-7/workload.json --output out/paged-replay
python -m pytest regression_tests/test_paged_attention.py -q
python -m pytest regression_tests -q
```

公开测试是有限示例，完整 workload 包含持续 checkpoints，需同时核对中间画像、真实生成、在途占用和后续请求。speculative/grammar、SSE、metrics 与 replay 在同一工作负载中验证集成结果；旧入口做回归检查。不要求固定工具调用次数或等待时长。
