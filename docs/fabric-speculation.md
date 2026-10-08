# 量化张量分片、采样与 speculative KV 契约

本文件与 measurement、deployment、admission、protocol 同时生效。新模式修复实际
推理数值路径；CPU 后端提供量化后的 target/draft logits，不提供网关应输出的答案。
原有 prompt 测量仍用 Unicode codepoint 计数；新模式的**生成 token**使用部署词表，
一个 token 可只是 UTF-8 字符的一部分，不能再用字符数代替已提交 token 数。

## 请求与部署资产

decode_mode="speculative" 启用本模式；缺省为原三阶段 SSE。未知 mode 返回 400。
temperature 缺省 1，必须为有限正数；top_p 缺省 1，范围 (0,1]；top_k 缺省 0，
为非负整数，0 表示全部，大于词表长度等价于全部；logit_bias 缺省空对象，
键为无前导零的十进制 token ID 字符串，值为 [-100,100] 的有限数。bool 不算数值。
上述类型/范围错误在派发前返回 400；词表范围由接纳时的解码部署决定，越界 bias
在处理后端数值时属于 decode error。原 model/version/tenant/deadline 约束仍生效。

fabric.json 的 decode_assets 指向 JSON 资产目录，按 deployment_id 索引，包含
deployment_id、generation、tokenizer_revision、quantization、eos_id、vocabulary、
shards。vocabulary 为按 token ID 排序的十六进制字节串列表，EOS 对应空字节串。
shards 为按 rank 排列的分片，每项含 token_ids、正有限 scale、非负整数 zero_point。
所有 rank 的 token_ids 必须恰好覆盖词表一次；rank 的局部列顺序不等于全局 token 顺序。
资产身份须与选中 decode deployment 相符。初始化/reload 时完整校验，非法更新
整体拒绝，不得部分换代；内部校验函数、缓存结构和文件拆分不受限制。

接纳捕获资产、采样参数和三阶段部署快照。speculative 请求始终携带 deployment
fence，承载方式遵守 deployment 文档；原请求参数继续传给 prefill/decode。
reload 不改变已接纳请求的词表、量化参数、fence 和 feedback epoch。

## 后端事件与客户端输出

decode 后端使用 SSE data: JSON，LF/CRLF 均有效，网络字节边界任意。
注释与空白可穿插；传输层不能假设一个 read 就是一条消息。所有 JSON 事件共同含
request_id、deployment_id、generation、tokenizer_revision、window、kind，身份必须
匹配该请求的接纳快照，window 为从 0 连续增长的非负整数。允许附加审计字段。

一个窗口为 begin、各 rank 的 shard、commit：

- begin：position 为此前已提交 token 总数，proposal 是 1–8 个有效 token ID，
  uniforms 长度为 proposal 长度加一，各值在 [0,1)。同一请求最多一个未提交窗口。
- shard：rank、target、draft。若 proposal 长度为 k，本 rank 的 target 矩阵
  为 (k+1)×本地词表宽度，draft 为 k×本地词表宽度，元素为整数 uint8。
  rank 到达顺序不定；完全相同矩阵的重传幂等，忽略附加审计列。相同 rank 的冲突、
  非法 rank/shape/value 均立即失败；完整性不能靠收包次数判定。
- commit：只有所有 rank 均齐全且一致时可以提交。不得在 commit 前向客户端暴露
  草稿、概率、logits 或 token，也不得因此记录首文本。

客户端仍收到普通 completions SSE：choices 中 text 为已提交字节解码出的文本，
token_ids 为本次提交的 token ID。允许将同一提交拆成多个客户端消息或合并相邻
提交，最终已提交 token/text 的顺序和内容必须正确；允许额外审计字段，不限制空
消息和 JSON 键顺序。原始 backend kind/matrix 不能作为客户端结果透传。

EOS 与 max_tokens 均会截断本窗口结果，并禁止后续 begin。EOS 自身计入已提交
token 数，不产生文本。仅 EOS 的成功请求没有 TTFT；TTFT 从接纳到首次非空
已提交 text，包含跨窗口字节拼接所需时间。第一批已提交 token 不一定产生文本。
正常结束必须收到独立 [DONE]，且已经达到 token 上限或 EOS、没有未提交窗口、
没有未完成 UTF-8 字符。DONE 后只能空白/注释；其他事件为 error。途中关闭、
半字符、缺 rank、错身份、过期 position、非法矩阵等也为 error。已返回 HTTP 200
的流不补发 502；终态、资源和监控应体现失败。失败后不继续消费后续窗口。

## 必须保持的推理分布

rank 内每个量化值先按 (value-zero_point)*scale 还原，并映射回全局 token ID。
每行 target 和 draft 都独立适用相同采样参数：先加 logit_bias，再除 temperature，
再取 top_k；同值以较小 token ID 优先。softmax 应数值稳定。随后按概率从大到小
（同值仍按 ID）取累计概率首次达到 top_p 的最小前缀，最后归一化。
rank 本地 softmax、先做 nucleus 后做 temperature、拼接局部列都会改变分布。

对第 i 个 proposal token t，记其 target/draft 分布为 p_i/q_i。必须 q_i(t)>0；
接受条件是 uniforms[i] < min(1,p_i(t)/q_i(t))，等号属于拒绝。在首个拒绝处，
从归一化 max(p_i-q_i,0) 用最后一个 uniform 采样一个替换 token，并丢弃该
窗口后续草稿。残差总量为零时使用 p_i。全接受且没有 EOS/预算终止时，从最后
一行 target 用最后一个 uniform 采样一个 bonus token。按 token ID 升序逆 CDF，
选择累计概率严格大于 uniform 的首个 token。每次拒绝后下一窗口从实际已提交
position 继续，不能从原 proposal 长度继续。

这里只规定可观察的数值语义与不变量；实现可选向量化、不同状态结构和不同模块，
不要求沿参考代码的算法组织方式。数值输出容差 1e-5，token 选择必须一致。

## 页所有权、终态与监控

原 admission 的 prompt+max_tokens 页保持预留。begin 额外原子预留
ceil(k/page_tokens) 个 scratch 页，占所选 decode 资源及同租户 max_pages；
外部有效快照和其他在途请求同样参与容量判断。scratch 不新增 slots/work 账单。
不足时窗口失败并终止请求，不透支、不悄悄缩短草稿、不转移到别的部署。
所有成功接纳的窗口计 proposed；提交时才记 accepted、corrected、committed、windows。
accepted 是实际保留下来的草稿 token 数；corrected 是实际输出的替换或 bonus 数；
committed 包含 EOS。拒绝时丢弃的后缀、尚未 commit 的窗口不计入已提交结果。
每次 commit 释放该窗口全部 scratch；error/cancel 释放尚持有 scratch 和原预留，
只释放一次。取消与 frame 同时发生时先取消，旧窗口不能复活。

/fabric/diagnostics 在存在 speculative 请求时提供
speculation:{request_id:{proposed,accepted,corrected,committed,windows,scratch_pages}}，
从接纳时的全零状态持续可见，终态保留累计值但 scratch_pages 为 0。
nodes.pages 包含 scratch；requests.pages 仍为原 admission 预留，含义不变。
每个 decode 资源提供 fabric_speculative_<上述字段>{resource="..."}，为该资源
请求的累计值或当前 scratch 合计，包含零值，不使用 request/tenant 作为 label。
错误或旧 epoch 的 decode 不污染当前校准；只有成功且已产生首文本才反馈 decode。

## 回放和验收边界

新 workload 的 decode_frames 是外部后端时间表，每项为 {at_us,wire}。
frame 事件采用旧 first 事件的排序位置：cancel、end、first/frame、transfer、
prefill、reload、observation、cache、arrival、checkpoint；同类请求仍按 ID 排序。
它们不能作为网关决策输入。回放需从真实 HTTP 响应解析
outputs:{request_id:{text,token_ids,done}}，仅列 HTTP 200 且未取消的请求；
其他 checkpoints/statuses/metrics 与旧协议相同。资产与全部原始证据均由输入
目录提供，私有验收更换分片布局、量化参数、logits、采样参数和事件时序。
验收直接观察独立 transport、HTTP 内容和持续账本，不限制新增 helper 的名字。
reload 时间表中的 asset_override 为外部部署导出故障：identity 指定资产，
path 是对象键/数组下标组成的路径，value 是临时错误值。回放在 reload 前写入
该资产，要求 reload 拒绝并保持原状态，随后恢复原文件；其后的合法在途请求
仍继续。异常类型不作限制，也不要求增加新的控制方法。
