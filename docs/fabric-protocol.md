# 三阶段后端协议和持续回放

实际转发须保持 X-Request-Id，与接纳组合一致：

1. POST prefill URL + `/v1/prefill`，JSON 为原请求加 cached_tokens。
   ack `{request_id,resource,layout,kv_handle}`，resource 为 prefill ID。
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
transfer、prefill、reload、observation、cache、arrival、checkpoint；同类请求按 ID，
其他记录按数组位置。相同时刻到达批量并发启动，接纳须保持原子。
被拒绝或终止的请求后续服务事件没有效果；没有人为休眠模拟 service 成本。

公开 replay 的 phase backend 用各请求独立 Event 控制异步边界，timing 不等同
于回放事件的 wall-clock 时长；fail_phase 是指定阶段故障，bad_ack 是错身份。
decode 输出 comments、空 text 和拆分的中文首 text。需要核查旧 pilot 的屏障和
终态行为与真实协议一致，不能只改回放生成看起来正确的报告。

`fabric-evaluation.json` 为 `{checkpoints,statuses}`。checkpoint 含 at_us、完整
diagnostics 及 dispatch。dispatch 按 id/url 排序，每项 `{id,resource,url,body}`
记录已发生的真实后端调用。statuses 为 HTTP code 或 cancelled；对流错误，
HTTP 与终态的差异应如实保留。metrics.prom 保存最终真实 metrics。
CLI 必须从该目录原始测量生成所需 profiles，再通过实际 HTTP 入口驱动回放。

实际接纳不依赖未来的回放事件、timing 或 fail_phase；这些只属于外部模拟后端。
公开测试材料里不会提供验收答案或故障原因表。私有验收直接驱动 ASGI 并拥有
自己的 transport，不信任 candidate 的 replay 自报。
