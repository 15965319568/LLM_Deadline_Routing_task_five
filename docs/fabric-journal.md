# 可回放状态日志与审计支线

V7 的在线状态不仅要能在当前检查点对账，还要能解释“哪个输入在什么时刻改变了
状态”。`FabricGateway` 的 `/fabric/diagnostics` 必须额外返回 `journal`：

```json
{
  "count": 12,
  "head": "<sha256>",
  "events": [],
  "audit_counts": {"observation:future": 1}
}
```

`events` 是只追加的提交链。每条事件含 `seq`、`at_us`、`kind`、`id`、`payload`、
`prev_hash`、`hash`。第一条的 `prev_hash` 是 64 个零，`seq` 从 1 连续递增。
`hash` 是把不含 `hash` 的整条对象按 `ensure_ascii=false`、`sort_keys=true`、
`separators=(',', ':')`、`allow_nan=false` 编成 UTF-8 JSON 后取 SHA-256。`head`
必须等于最后一条 hash，空链的 head 仍是 64 个零。

允许的 kind 是 `admission`、`rejection`、`phase`、`first_token`、`terminal`、
`reload`、`topology`、`cache`、`observation`。接纳或拒绝每个 request ID 只能
有一条 decision 事件；接纳事件的 payload status 为 200，拒绝事件为真实的
400/409/429/503。接纳后的后端错误可能把客户端 HTTP 结果变成 502，取消也仍
保留原来的 200 admission 事件，并且必须有且只有一个 terminal 事件。有效的
prefill/transfer ack、首 token 和终态结算分别追加 phase/first_token/terminal
事件。重复终态不得重复追加。

reload 的 prepare/commit/abort、拓扑更新、缓存替换和被接受的外部快照追加对应提交；迟到、未来、重复、
冲突、非法或过期的观测只能增加 `audit_counts`，不能增加提交链。audit key 只能
是小写的 `source:reason`，不能包含 request ID、trace ID 或其它高基数字段。审计
计数用于诊断，不得作为租户或请求标签写入 Prometheus。

事件按 gateway 实际处理顺序生成，不能在 `inspect()` 时按字典序重新排序，也
不能从 workload 的未来 timing 预先生成。`replay` 产物必须带同一 journal；验收
会对 live/replay 两条链逐条重算哈希，并用五组不同输入检查乱序、重复、reload、
cache、topology、取消和阶段失败。
