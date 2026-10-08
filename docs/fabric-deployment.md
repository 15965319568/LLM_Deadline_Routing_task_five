# 推理部署事实与跨阶段兼容契约

fabric.json 声明物理资源、角色、有向连接、受支持的模型兼容条件和默认请求
deployment_contract。资源内的 deployment_id/generation/config_hash 是旧启动快照；
有 deployments 导出时，实际生效身份必须从证据重建，不能直接抄启动快照。
phase-exports.json 的 deployments 列出同等有效的 CSV、JSONL 和 gzip 文件。

每行 source_kind 为 registry/runtime/rollout。三者共同包含 resource、deployment_id、
generation、model_revision、tokenizer_revision、quantization、kv_schema、adapter、
config_hash、effective_us、ingested_us；runtime 另有 loaded_us、ready，rollout 另有 state。
非空字符串不猜测修正；generation 和时间是非负整数，数字字符串等价，bool 不算整数。
ready 接受 JSON bool 以及 CSV 的 true/false/True/False，不接受数字 0/1。
state 为 active/shadow/retired。CSV 空的非本类型字段忽略，未知附加审计列不参与身份。

截止时刻先裁 ingested_us，可见时间非法记 invalid，尚未可见记 deferred。
可见行缺必要字段、坏 JSON、非对象、未知 resource/source_kind 或非法值记 invalid。
有效行按 (resource,deployment_id,source_kind) 对账：忽略 ingested_us 和无关列，
将该来源类型的上述字段规范化后比较。重复只保留来源字典序最小的一行 candidate，
其余 duplicate；不同事实为 conflict，整个 identity 不供后续使用。未来副本不能
提前制造冲突。来源行号沿用 measurement 的实际行号约定。

同一个 resource/deployment_id 必须同时存在无冲突 registry、runtime 和 rollout。
generation/model_revision/tokenizer_revision/quantization/kv_schema/adapter/config_hash
在三方须相等；ready 为 true，loaded_us 不晚于截止，三方 effective_us 均不晚于
截止，rollout.state 为 active。registry 的五项兼容选择字段须符合该物理资源在
fabric.json 的支持条件。未通过联结的 registry candidate 改为 excluded；其他行
仍保留其局部裁决，不把整个导出笼统标记为失败。

每资源从合格联结中取最大 (三方 effective_us 的最大值,generation,deployment_id)。
仅 registry 最新、仅 runtime ready、仅 rollout active 都不足以证明可服务。
无合格资源时 build 可省略其部署条目，但 gateway 启动/reload 必须拒绝不完整集合。

build 新增 deployment-profile.json：
`{as_of_us,resources:{id:{resource,deployment_id,generation,config_hash,model_revision,
tokenizer_revision,quantization,kv_schema,adapter,effective_us}},ledger:[{source,disposition}]}`。
effective_us 为三方生效时刻的最大值，ledger 按 source 排序、每原始记录一个结果。
额外审计字段允许；不得少报输入记录。历史合格 candidate 不因另一个部署胜出而
改成 conflict；最终 resources 决定真正生效身份。

测量必须属于选中 deployment_id，且 model_revision/tokenizer_revision/quantization/
kv_schema/adapter 与其一致；其余 steady、时钟、trace、分页等条件同时成立。
新部署激活前可收集 canary 测量，但只有该部署成为当前服务身份后才纳入它的画像。
不能把旧部署的样本继续并入新部署 support，也不能从旧 deployment 补齐 null。

请求可带 model_revision、tokenizer_revision、quantization、adapter 和 kv_schema。
前四者未填写时按 deployment_contract 逐字段取缺省；kv_schema 未填写时按候选
布局求兼容。任何显式空值/非法类型为 400，合法但无匹配组合为 503。路径三资源
须同时满足选择字段及原有 layout/source/target 约束。session 亲和仍以路径为单位，
不能绕过版本条件。模型名称相同不代表版本兼容。

带 deployment_id 的 KV lease 是部署限定页：必须同时具有并匹配五个兼容字段、
deployment_id 和 generation。page_hash 仍校验 layout/session/page_index/tokens，
它不能代替部署资格；同样的 tokens/hash 在旧 generation 也不能复用。旧无
deployment_id 的 lease 保持原 namespace/血缘规则，兼容字段缺省继承接纳条件。
离线按样本所用部署裁决，在线按接纳时所选 prefill 部署裁决；tombstone 同时生效。

gateway 启动与 reload 一并校验 profiles 和 deployment-profile，非法更新全量拒绝。
资源画像语义变化或选中部署对象变化都推进该资源 epoch，并重置其校准和采样去重；
只有 source ledger/support/as_of 变化不推进 epoch。成本完全相同的部署替换也要推进。
附加审计字段不属于部署或资产语义身份，单独增加/修改它们不推进 epoch；
比较选中部署与 decode 资产时仅使用本文件及 speculation 文档列出的语义字段。
接纳捕获三资源的完整部署身份、baseline/reserved/epoch；在途请求继续按该快照
完成 ACK 和后续派发，禁止读取 reload 后的新身份去验证旧事务。旧 epoch 反馈
不进入新部署校准；未改变资源仍可接收旧请求反馈。同一合法更新重复装载幂等。

显式携带任一兼容选择字段的请求启用部署 fencing：每阶段派发传目标资源的
X-Deployment-Id、X-Deployment-Generation、X-KV-Schema headers。等价 JSON 承载
target_deployment_id、target_generation、kv_schema 也接受；含义是该阶段目标，
generation 为整数或整数 header 字符串。prefill/transfer ACK 另回显
deployment_id/generation/kv_schema，必须匹配接纳快照；错误 ACK 返回 502、阻断
下游、清理全部预留并熔断整条路径。decode 派发也必须使用其接纳身份。

显式请求的 transfer JSON 在原字段上增加源 prefill 的 deployment_id/generation/
kv_schema，标明 KV 生产身份；目标 link 身份在上述 header 或 target_* 字段中。
prefill/decode 保留原请求所有字段，允许附加审计数据。dispatch 每项仍记录真实
id/resource/url/body，显式请求另给 deployment:{deployment_id,generation,kv_schema}，
为目标阶段身份的规范化视图，来自实际发出的 headers/JSON，不可自报虚构。
没有显式选择字段的旧请求保持原 wire 协议，但接纳仍必须遵守默认版本与缓存资格。

workload 的 backend_deployments 是独立后端用来模拟该请求所连接服务的部署事实，
bad_deployment 指定阶段返回错误 generation；网关不得读取 workload、未来事件
或测试故障开关作接纳决策。公开 replay 和独立验收 transport 均遵守相同接口。

新 speculative 解码同时遵守 fabric-speculation.md；其生成 token、窗口页与终态规则适用于 decode_mode="speculative"，普通 SSE 保持本文件语义。
