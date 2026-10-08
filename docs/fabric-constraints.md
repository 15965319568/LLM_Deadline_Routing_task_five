# 有状态约束采样、语法版本与停止串

本文件与其余六份 fabric 契约同时生效。结构化输出服务把语法编译导出、量化
部署和 speculative 解码同时上线。修复目标是实际生成 token 的分布、文本与
KV 提交保持一致；不是生成一份语法分析报告。内部文件、类和算法组织不受限制。

## 从原始语法导出构建部署产物

fabric.json 的可选 grammar_exports 指向清单 JSON，sources 为 {path,format}
数组，format 为 csv、jsonl 或 jsonl.gz。每条记录含 grammar_id、revision、part、
event_us、ingested_us、payload。revision 为正整数，两种时间为非负整数，bool
不是整数；grammar_id 为非空字符串，part 为 metadata/arcs/release。CSV 的整数
列按十进制解析，payload 是 JSON 文本；JSONL 直接使用 JSON 类型。额外审计列无语义。
payload 必须为对象或数组。每条记录的 source 为 `<清单相对路径>#<记录序号>`，
序号从 1 起，CSV 不计表头，JSONL 不计空白行。输入格式可解析，但业务内容可能非法。

身份或上述基础类型非法为 invalid；其后任一时间超过 build cutoff 为 deferred，
未来记录不参与当前冲突。按 (grammar_id,revision,part) 聚合可见合法记录。
语义由 event_us 与 payload 组成，ingested_us 和审计列不参与比较；有不同语义则
组内全部 conflict。完全相同的副本保留 source 字典序最小者，其余 duplicate。

每个 revision 必须同时具有三个无冲突的部分：

- metadata：tokenizer_revision 非空字符串；states 为非空、不重复非负整数列表；
  start 属于 states，accept 是 states 的子集；base_revision 为 null 或较小正整数。
- arcs：有序补丁列表，每项 {op,from,byte,to}，op 为 add/remove，byte 为 0..255
  整数。无 base 时从空转移表开始；否则基于同 grammar、同 tokenizer 的有效且已
  release 的 base_revision。逐条应用补丁；add 相同边可幂等，覆盖不同目标非法；
  remove 必须精确匹配已有 from/byte/to。最终所有状态须属于 metadata.states。
- release：enabled 必须为布尔 true 才可使用。

一个 (from,byte) 只能有一个目标。缺部分、错误补丁、禁用、非法图、缺失/冲突/
未发布父版本、跨 tokenizer 继承均使该 revision 不可用；可以退回较旧的有效版。
每个 grammar_id 选择最大有效 revision。有效旧版本的代表行为 shadowed，选中
版本三条代表行为 selected；其他未选中的代表行为 excluded。已有 duplicate、
conflict、deferred、invalid 分类不因选择结果改写。

此类数据集的 build 额外交付 grammar-profile.json：
{as_of_us,grammars:{grammar_id:{revision,tokenizer_revision,states,start,accept,edges}},ledger}。
states/accept 升序且 accept 去重；edges 是展开后的完整 {from,byte,to}，按 from/byte
排序；ledger 为 {source,disposition} 列表，按 source 排序。允许附加审计字段。
这是第六项 build JSON，缺少 grammar_exports 的旧输入仍只要求原五项。

初始化及 reload 从同一 profile 目录加载此产物，拒绝未来 cutoff、非法完整图或
缺失产物，更新必须原子。不得在线直接信任未裁决的导出或 notebook。
与某 decode 部署 tokenizer_revision 相同的有效语法集合发生语义变化时，该
decode 的 epoch 更新一次，清空该资源反馈；同次成本/部署变更不重复加 epoch。
prefill/link 不因纯语法变化而更新。额外审计字段不构成语义变化。

workload.reloads 可用 profile_override 模拟部分写入的发布目录，字段为
{file:"grammar-profile.json",path:[对象键或数组下标,...],value}。回放临时修改
该次 build 的指定产物，要求 reload 拒绝且保持旧状态，随后恢复原文件；这与
旧 asset_override 一样属于外部故障输入，不是网关决策可读取的未来信息。

## 请求参数、接纳和部署快照

以下可选参数用于 decode_mode="speculative"，缺省值保持旧行为：

- grammar：非空 grammar_id 字符串；省略表示无语法，显式 null 为 400。
- prompt_token_ids：非负整数列表，默认 []，用于采样惩罚历史，不替换 prompt 的
  物理计费口径。选中部署词表越界或包含 EOS 返回 400，且不派发、不记开始次数。
- repetition_penalty：有限数 (0,10]，默认 1。
- presence_penalty、frequency_penalty：有限数 [-2,2]，默认 0。
- min_tokens：非负整数，不超过 max_tokens，默认 0。
- stop：最多四个非空字符串组成的列表，默认 []；每个 UTF-8 编码不超过 64 字节。

bool 不算数值，类型/范围错误返回 400。语法不存在或没有 tokenizer 相容的完整
路径返回 503。其他已公开的接纳和配额规则仍生效。接纳冻结选中的语法、词表、
采样参数和历史；reload 不改变在途请求。相同 grammar_id 的新 revision 也不能
替换旧请求的状态。仅 decode 派发携带 X-Grammar-Id/X-Grammar-Revision，或等价
JSON 字段 grammar_id/grammar_revision；取接纳快照。无 grammar 时不要求它们。
replay 的对应 decode dispatch 记录增加 grammar:{grammar_id,revision}，由实际
派发规范化取得，仍允许额外审计信息。

## 分布与草稿提交

语法是字节 DFA。生成开始于 start，prompt_token_ids 不推进语法。普通 token
只有其 vocabulary 字节能从当前状态逐字节走完才合法；EOS 只有当前状态属于
accept 且已经生成至少 min_tokens 时合法。没有语法时仍应用 min_tokens。

每个实际处理的位置均使用 prompt_token_ids 加本请求此前实际生成 token 的
计数；同窗口此前接受的 token 已属于该前缀，未接受后缀不属于它。处理次序为：
反量化并映射全局 token → logit_bias → 对历史出现过的 token 应用 repetition
惩罚（正 logit 除以系数，非正 logit 乘系数）→ 减 presence_penalty（出现过）
和 frequency_penalty×出现次数 → 去除不合法 token → 原 temperature/top_k/
top_p。去除发生在 top_k 之前；空合法集合为 decode error。

同一位置的 target/draft 使用同一实际前缀，继续遵守旧接受比、残差替换与 bonus
规则。q_i(proposal_i)=0 属于 error。首个拒绝后后缀不再参加数值采样；下一窗口
从实际替换 token 的语法状态和历史继续。bonus 使用本窗口已接受全部 token 后
的状态。原始矩阵的所有 shape/value/rank 仍须先合法，不因后缀未用而忽略损坏。
一个窗口的历史、语法、token、停止状态和输出共同提交；错误窗口不能留下其中
任何部分变化，之前窗口的提交保留。begin 的 proposed 与 scratch 所有权沿用旧规。

## 文本延迟、停止串与终态

stop 按生成的原始字节匹配，允许跨 token 和窗口。每个选出的 token 按序追加后
检查匹配；取最早结束位置，同结束位置取最长 stop，再按 stop 列表顺序。匹配
之前的字节可输出，stop 本身及之后字节不输出。包含匹配结束位置的整个 token
仍计入生成/历史/KV 提交；同窗口后续 token 不再生成，reason 为 stop。

尚未匹配时，保留当前字节后缀中作为任意 stop 前缀的最长部分，同时保留尚未
组成完整 UTF-8 的字节；仅输出此前完整 UTF-8 文本。EOS/预算先结束时，未匹配
的 stop 前缀按普通文本冲刷，UTF-8 必须完整。EOS、length、stop 均禁止新 begin。
停止匹配、EOS 和预算都需要最终 [DONE]，不能凭文本看起来完整就记成功。
同 token 同时达到 stop 和预算/EOS 时 stop 优先；否则 EOS 优先于 length。
stop 不绕过语法 token 完整合法性。TTFT 只按实际交付的非空 text；token 已提交
但文本被停止前缀暂存时仍没有 TTFT。取消/error 丢弃未输出字节并清理全部资源，
不把停止前缀强行输出。未提交窗口继续遵守原子回滚。

diagnostics 增加 decoding:{request_id:{grammar_id,grammar_revision,state,
generated_tokens,history_counts,pending_bytes,stop_reason}}，从 speculative 接纳起
持续可见。无 grammar 的前三项为 null；history_counts 键为 token ID 十进制字符串，
值为 prompt 历史加已提交生成计数，省略零值；generated_tokens 包含 EOS 和导致
stop 的 token。pending_bytes 为尚未交付的字节数，所有终态为 0；stop_reason 为
null/eos/length/stop，error/cancel 不编造采样终止原因。其他监控指标仍遵守旧契约，
不新增请求/租户标签。这些状态是公开行为，不要求任何固定内部容器。

## 树状草稿与分片重排

同一 speculative 请求可以在窗口间切换线性草稿与树状草稿。树窗口的 begin
保留公共身份、window 和 position，以 tree 取代 proposal，不能同时包含二者。
tree 为 1..32 项 {id,parent,token}；id 为不重复正整数，根 0 不在列表中，parent
为根或已声明节点，不要求按拓扑顺序排列。节点必须全部连通到根、无环，根到
任何节点的边数不超过 8；同一 parent 的子节点 token 不重复，token 必须在词表内。
uniforms 为对象，键恰好为根及所有节点 ID 的标准十进制字符串，值在 [0,1)。

树 shard 含 rank、node_ids、target。node_ids 必须恰好覆盖根与全部树节点一次，
target 每行与 node_ids 对应，列与该 rank 的 token_ids 对应，元素为 uint8。
每个 rank 可独立排列行；同一 rank 重传改变行顺序但语义相同仍幂等。先按节点
身份比较，不能按矩阵位置认定冲突。缺行、重复行、冲突、坏值或坏形状立即失败，
也要检查未命中分支的矩阵。树 shard 不要求 draft；附加审计列无语义。

树表示预先计算好的多条候选 KV 路径。commit 从根开始：使用当前节点的 target
logits、实际生成前缀的历史/语法/停止状态及该节点 uniform 按既定分布采样。
若采出的 token 命中当前节点的一个子分支，记 accepted 并沿该子节点继续；
若未命中则记 corrected，输出该 token 并结束本窗口。叶节点同样采样一次，
因没有子分支而形成 corrected token。任一 token 触发 EOS/预算/stop 则立即结束，
不继续读其他节点的 uniform。这里直接验证 target 采样是否命中已计算的草稿
路径，不对树使用线性 proposal 的 p/q 接受比；线性窗口保持旧规则。

begin 原子预留 ceil(全部树节点数/page_tokens) 个 scratch 页，proposed 增加全部
节点数；不能只预留最终命中路径。节点数不计根。选择路径的 token 才更新历史、
语法和已提交位置。未命中分支不生成文本，不进入历史。commit/error/cancel 回收
全部树 scratch，原请求账单和原 KV 预留仍遵守各自生命周期。窗口任一失败不提交
其部分 token/历史/状态；此前窗口保留。树窗口的客户端消息、TTFT、diagnostics、
并发配额、在途版本快照和 replay 均与线性窗口使用同一套公开语义。
