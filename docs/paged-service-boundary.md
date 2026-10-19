# 提供的服务基础与本题验收边界

本题修复对象由 `paged-kv-contract.md` 定义。画像编译、部署证据裁决、通用接纳算法、SSE 封帧、语法与采样算法、完整 metrics/replay、旧 CLI 不是独立修复或交付项目。基础实现已完成，仓库其他说明不增加验收义务。可以使用或重构它们，但最终分页服务仍须遵守本文件的公开行为。

## 输入和公开入口

`captures/paged-session` 和 `captures/shared-session` 分别提供 fabric.json、decode-assets.json、workload.json 和 profiles/initial、profiles/rotated。profiles 已是有效部署与成本快照，无需从原始测量重新 build；测试直接加载这些文件。

公开构造为 `FabricGateway(source_dir, profile_dir, clock=..., transport=...)`，从 `serving_lab.fabric` 导入。`.app` 是 ASGI 应用。`await gateway.reload(profile_dir)` 是本题使用的唯一控制接口，发布给定有效快照，在途请求保持原快照。`ManualClock` 从 `vllm_router.deadline` 导入，提供 now_us()/advance_to(at_us)。以上名字属于公开测试接入面；PageContext、内部辅助函数、文件和状态组织不受限制。

HTTP 入口为 POST `/v1/completions`，以 X-Request-Id 标识请求；GET `/fabric/diagnostics` 提供持续观察。示例 body 已给出模型/部署选择、prompt、max_tokens、deadline_us、tenant、stream、decode_mode="speculative" 和 decode_kernel="paged-gqa"。这些 mode 名称属于既有消费者协议，不意味着要实现全部 speculative 功能。无 decode_kernel 的 logits 请求只用来观察同一容量池的接纳结果。

已有 gateway 在接纳时冻结完整资产，通过以下真实阶段转发：prefill `/v1/prefill` → link `/v1/kv-transfer` → decode `/v1/completions`。transport.request 是异步 context manager，接收 method/url/headers/json；响应提供 status、headers、json() 或 content.iter_any()。测试辅助文件给出完整可运行 transport。每个接纳的示例请求执行各阶段一次；被拒绝的请求不能发出后端请求。工作负载不要求重试机制。

部署身份可以由 X-Deployment-Id、X-Deployment-Generation、X-KV-Schema headers，或协议的 target_deployment_id、target_generation、kv_schema JSON 字段携带；header 大小写不影响语义。ACK 和 KV handle 处理由已有实现提供。

## 作为给定依赖的行为

配置提供有效资源/租户容量、模型兼容路径及 profiles。通用接纳与草稿 scratch 已实现；分页工作区应通过既有容量机制参与约束。资源和租户均不能透支。容量不足的后续请求返回 429；已进入流的页分配失败体现为流 error。终态释放后可服务的下一请求恢复接纳。

采样与字节消费者已提供可工作的实现，窗口用当前数据生成 token/text。各场景不要求新增语法、树草稿、停止串或惩罚功能。分页计算影响下游结果，因此真实输出是核心验收证据；测试不直接调用候选人的私有数值函数。

错误/取消不撤销此前成功提交的 token；错误窗口不输出部分结果。HTTP 200 已发送后不改写状态码；诊断 outcome 表明 success/error/cancelled。取消的客户端请求标为 cancelled。完整成功仍收到 [DONE]。分包、注释和 CRLF/LF 由提供的 SSE 实现处理；分页原始 page/query 矩阵不得作为客户端结果直接透传。

## 持续驱动和验收字段

workload 的 requests 包含到达、prefill/transfer 完成、每个 decode frame 的时刻以及可选 cancel_us；reloads 指向给定 profile 目录；checkpoints 为观察时刻。时间单位均为微秒。相同时刻按 cancel、frame、transfer、prefill、reload、arrival、checkpoint 顺序，同类请求按 ID，控制记录按原数组顺序。同刻请求并发接纳。外部时间表不应成为生产决策读取的未来信息。

独立 transport 会控制每个阶段和帧到达，并直接观察真实派发；不信任候选人自报 replay。验收投影仅包括：

- 各请求的 HTTP 状态/取消，以及客户端按顺序收到的 token_ids、拼接 text、是否收到 DONE。允许空消息、合并或拆分提交及额外审计字段。
- checkpoint 的 `nodes[resource].pages`，包含原请求预留、草稿 scratch 和分页工作区。
- `paged_attention[request_id]` 的分页契约字段。
- `requests[request_id].outcome`，在途为 null，终态为 success/error/cancelled。

不比较整份 diagnostics，不要求完整 metrics、replay 输出、build 产物或任何旧接口套件通过。对象按声明字段比较，额外字段合法；数组中声明的顺序有语义。公开示例覆盖同一条服务路径，私有输入更换布局、页内容、分片选择/重传/冲突、继承关系、逻辑视图、共享来源、压实依赖及交错时序。共享与压实的容量接合由解题者修复；通用接纳公式仍作为给定依赖。分片和整页均属于同一分页输入协议。公开 tests 可以直接运行，不要求修改测试使其通过。
