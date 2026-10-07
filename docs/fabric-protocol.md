# 三阶段后端协议和持续回放

实际转发须保持 X-Request-Id，与接纳组合一致：

1. POST prefill URL + `/v1/prefill`，JSON 为原请求加 cached_tokens。
   ack `{request_id,resource,layout,cached_tokens,kv_handle}`，resource 为 prefill ID，
   cached_tokens 必须等于接纳时的连续页证明。
2. POST link URL + `/v1/kv-transfer`，JSON 为 `{request_id,source,target,kv_handle,bytes}`。
   handle 是 prefill ack，bytes 是完整 prompt KV。ack 为
   `{request_id,resource,target,layout,kv_handle}`，resource 为 link ID。
3. POST decode URL + `/v1/completions`，JSON 为原请求加 transfer ack 的 kv_handle；
   原样转发 SSE。首 text 判断遵守接纳契约。

prefill/transfer 必须为 200 且 ack 身份、resource、layout（transfer 另含 target）
与接纳一致，handle 非空；否则前两阶段返回 502、清理全部预留、后续阶段不发。
decode 的错误/断流也清理，不能记录成功反馈。已经发给客户端的流不能补发
502；检查流状态及终态监控。取消不会继续启动下一阶段，所有权只释放一次。
后端 timing headers 为 x-service-sample/x-service-us。不能信任 x-baseline-us。

workload.json 是自然请求与服务事件，不是评分脚本。window、builds/initial、
requests（at/prefill/transfer/first/end/cancel 的绝对微秒）、reloads、observations、
caches、checkpoints 共同描述 CPU 事件流。same-time 的顺序：cancel、end、first、
transfer、prefill、reload、observation、cache、topology、arrival、checkpoint；同类请求按 ID，
其他记录按数组位置。相同时刻到达批量并发启动，接纳须保持原子。
请求 body 中的 `tenant`、`priority` 和 `session_id` 属于接纳输入；回放必须把它们原样传给 prefill/decode。
`tenant`/`priority` 影响本地配额，`session_id` 影响缓存命名空间和路径亲和，不得作为 Prometheus label，也不得从 model 或 request ID 猜测。
缺省租户和优先级由部署配置决定；未知租户和越权优先级在发出任何后端请求前返回 400。
被拒绝或终止的请求后续服务事件没有效果；没有人为休眠模拟 service 成本。

公开 replay 的 phase backend 用各请求独立 Event 控制异步边界，timing 不等同
于回放事件的 wall-clock 时长；fail_phase 是指定阶段故障，bad_ack 是错身份。
decode 输出 comments、空 text 和拆分的中文首 text。需要核查旧 pilot 的屏障和
终态行为与真实协议一致，不能只改回放生成看起来正确的报告。

prefill acknowledgement 还必须回显 `cached_tokens`，且与接纳时的连续页证明
完全一致。decode 流只有在看到非空 text 且收到单独的 `data: [DONE]` 后才算成功；
连接在首 text 后没有 DONE、只发 heartbeat 或只发空 choices 都是 error。SSE 事件
可以跨任意字节边界，终止事件不能依赖一次 read 的完整性。

`fabric-evaluation.json` 为 `{checkpoints,statuses}`。checkpoint 含 at_us、完整
diagnostics 及 dispatch。dispatch 按 id/url 排序，每项 `{id,resource,url,body}`
记录已发生的真实后端调用。statuses 为 HTTP code 或 cancelled；对流错误，
HTTP 与终态的差异应如实保留。metrics.prom 保存最终真实 metrics。
CLI 必须从该目录原始测量生成所需 profiles，再通过实际 HTTP 入口驱动回放。

实际接纳不依赖未来的回放事件、timing 或 fail_phase；这些只属于外部模拟后端。
公开测试材料里不会提供验收答案或故障原因表。私有验收直接驱动 ASGI 并拥有
自己的 transport，不信任 candidate 的 replay 自报。

V7 的三次 ack 还必须包含 `transaction_id`、`phase` 和该阶段 `phase_token`；
请求 header 携带 `X-Reservation-Id`、`X-Phase-Token`、`X-Route-Id`、
`X-Route-Generation` 与 `X-Resource-Epoch`，并可用 `X-Topology-Epoch` 传递拓扑世代。
这些 V7 身份字段属于 header 协议，JSON 只需包含本节各阶段规定的业务字段；
为了兼容旧后端，JSON 中重复携带身份字段是允许的，但不是必需条件。route generation 或 token 不匹配时，
即使资源 ID 和 layout 看起来正确也必须回滚当前事务。阶段响应 headers 的反馈
身份是 `x-trace-id`、`x-phase-id`、`x-resource-epoch`、`x-sample-signature` 与
`x-service-*` 的联合签名；只接受当前 reservation 的对应阶段和 epoch。

回放事件还包含 `topology`。拓扑更新与 reload、cache replacement 和 arrival
共享同一事件顺序；路由排空影响新接纳，不能改变已经发出的后端 URL。cache 事件
可以带 `revocations`，撤销的 producer/generation/lease 组合即使 page_hash 正确
也不能形成缓存前缀。私有验收会重复、延迟和交叉这些事件，并检查 diagnostics
中的 route/transaction 身份和最终账本，而不是只看 HTTP code。

JSON ack 中 route_generation/topology_epoch/cached_tokens 必须是准确 JSON integer，
不能用字符串或 bool 代替；kv_handle 必须是非空字符串。decode 回执在 headers，
包含 x-ack-request-id/transaction-id/phase/phase-token/route-id/route-generation/
topology-epoch/resource/layout，值与接纳时固定值一致，generation/epoch 使用规范
十进制字符串，不接受前导零。无效 decode 回执使流终态 error，不采首 token；
客户端已经收到的 HTTP 200 不能改成 502。

reservation_generation 初始为 1；transaction_id 是
sha256(request_id+'|'+generation+'|reservation/v7') 前 24 个十六进制字符。
phase_token 是 sha256(transaction_id+'|'+phase+'|'+resource_epoch) 前 24 个字符。
默认 trace_id='trace-'+request_id，phase_id=request_id+':'+phase+':v7'。
feedback 签名是 sha256(trace_id+'|'+phase_id+'|'+sample+'|'+resource+'|'+epoch+
'|'+x-service-us 原始字符串)。编码均为 UTF-8。反馈必须同时满足签名正确、
对应请求的 trace/phase 正确、请求固定 epoch 与当前 resource epoch 一致。
签名正确但属于别的 trace/phase 的反馈仍要忽略，不应终止正常请求。

SSE 按严格增量 UTF-8 解码，支持 LF/CRLF、逐字节分片、多行 data 合并。完整事件
以空行结束。DONE 必须是独立且完整事件，之前必须已有非空生成 text；DONE 前
收到非法编码、损坏 JSON 或不完整事件使终态 error；DONE 后再有 data 事件亦为
error。注释与 heartbeat 不算 text。不能在 EOF 自动补齐缺少空行的 DONE。

cache revocations 含 lease_id/producer/generation/revoked_at_us/ingested_us。
generation 与两种时间必须是 type=int 的非负数（拒绝 bool、数字字符串）；身份
字段必须为字符串。只有 revoked_at_us<=接纳时刻 且 ingested_us<=可见性 cutoff
的合法记录，才按 (lease_id,producer,generation) 撤销。坏记录忽略；未来导出
不得提前撤销当前页，也不能导致整个请求异常。

状态变更日志和迟到输入的审计语义见 `docs/fabric-journal.md`，与本协议同时生效。
