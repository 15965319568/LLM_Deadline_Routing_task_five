# 持续流量回放协议

`python -m serving_lab replay --input DIR --workload FILE --output DIR`。
workload 是普通 JSON 运行数据，不包含可执行 Python 或测试谓词。

- builds：name、as_of_us。各画像从同一 raw input 构建到 output/profiles/name。
- initial：初始 build 名；window：[开始,结束] 微秒。
- reloads：at_us、build；更新可以发生在请求仍执行时。
- observations：at_us、rows（负载 snapshot 数组）。
- caches：at_us、claims、available。
- requests：id、at_us、body；first_us、end_us、可空的 cancel_us、connect_error；
  timing 的 sample、service_us、baseline_us 是后端响应头内容。时间是独立生命周期
  时刻，不由预测值反推。connect_error 表示后端连接异常，HTTP 为 500；其余正常
  响应 200。拒绝请求无后端。cancel 在 HTTP 已终止后无影响。
- checkpoints：需抓取当前状态的绝对时刻数组。

按时间推进 ManualClock。同刻顺序为 cancel、end、first、reload、observations、
caches、arrival、checkpoint；同类按原数组顺序，requests 在展开前按 id 排序。
同刻 arrival 按 id 顺序创建并发任务，统一等待这批接纳完成，不逐个等待后才创建。
每批 arrival 至少推进到其被拒绝/连接失败，或后端已发送 heartbeat/空 text 并等候
该请求自己的 first 信号；first 推进到非空文本被转发；end/cancel 推进到 HTTP
终止。不同请求不能共用 first/end 屏障。CPU 调度让出用 sleep(0)，时序由显式
clock 推进，不根据墙钟耗时决定结果。

生成 `routing-evaluation.json`，包含 checkpoints、statuses、summary。checkpoint
依次为 `{at_us,nodes,decisions,dispatch}`；nodes/decisions 与真实 diagnostics 相同；
dispatch 为至此后端实际观测到的 `[request-id,endpoint-id]` 对，按 pair 排序。
statuses 是 request-id→最终 HTTP 整数码或 cancelled。summary 使用整个 window。
同时写出 `/metrics` 的原始文本为 metrics.prom。命令输出与真实 HTTP 独立回放的
结果必须一致，不能按输入名字返回固化答案。
