# 导出格式和证据口径

目录由 `exports.json` 列举 measurements、spans、cache_claims 文件；允许 CSV、
JSONL 及 gzip 压缩版本，UTF-8 可带 BOM。文件排列、行排列不是事件顺序。
JSONL 空行不计输入，坏 JSON/非对象各计一条排除记录；CSV 用逻辑记录号，不含
表头。源坐标为相对文件名加 `#` 加从 1 起的记录号（JSONL 用物理行号）。
不要依赖固定分片名。`runs.csv` 的 run_id 唯一，运行字段允许外围空白。

运行清单包含六项完整条件：model、revision、tokenizer、accelerator、parallelism、
quantization。比较时转为去掉外围空白的字符串。还包含 purpose（baseline/recent）、
phase（measurement/warmup）、schema、available_at_us。只有 measurement 阶段
与部署 target 六项条件完全相同的运行可用于本题画像。多个相同条件 target 可共用
合格样本，每 target 产生一份派生样本；不同条件不得借样本补齐。

| 含义 | bench/1 | bench/2 |
|---|---|---|
| 运行、请求、尝试 | run, request, attempt | run_id, request_id, attempt |
| 提示文本 | prompt | prompt |
| 服务 span | span | span_id |
| 结果 | outcome | status |
| 客户端首 token 延迟 | ttft，秒 | client_ttft，微秒 |
| 可获知时刻 | delivered_us，微秒 | available_at_us，微秒 |

结果去外围空白、转小写。ok 且具有非负有限 TTFT 才可训练。超时的空 TTFT
合法地代表未知，排除而不补零；负数、NaN、Infinity 和布尔数值非法。attempt
是非负整数。未声明的提示计数/汇总字段不权威。codepoint-v1 以 Unicode 码点
作为 token ID；utf8-v1 以 UTF-8 字节作为 ID，均只使用本地 prompt 编码。

一次执行的身份为 `(run, request, attempt)`。只看 cutoff 时已可见且可规范化的
测量记录，规范化载荷（不含可获知时刻）相同的是重复投递；源坐标按字符串序最小
的一条代表执行，其余记 duplicate。相同身份的可见载荷冲突则所有记录记 conflict，
整次执行不用。相同 request 的不同 attempt 是真实重试，可独立贡献。重复的
可获知时刻取最早。可获知时刻晚于 cutoff 的测量或运行记录记 deferred，不参加
本次去重和冲突判断；其他无法解析或不合格的代表记录记 excluded。

| 含义 | span/1 | span/2 |
|---|---|---|
| span 标识 | id | span_id |
| 执行身份 | run, request, try | run_id, request_id, attempt |
| prefill 开始/完成 | begin_ms, end_ms，毫秒 | prefill_start_us, prefill_end_us，微秒 |
| 活跃 decode 数 | decoding | active_decodes |
| 缓存证明标识 | cached | cache_claim_id |
| 可获知时刻 | delivered_us | available_at_us |

span 的 schema 字段声明版本；complete 必须为 JSON true；结束不能早于开始，
decode 数为非负整数。以 span ID 合并可见重复，除可获知时刻外不同或存在有该 ID
的非法可见记录，则该 ID 不能作证。无法解释可获知时刻的已命名 span/claim 也使
其 ID 不可作证。未来的合法可获知时刻在本次忽略。测量必须引用身份完全匹配、
已完成且结束不晚于 cutoff 的 span。服务成本是 end-start；大于客户端 TTFT
说明证据不相容，该样本不用。合法的较慢服务记录不能按大小直接删掉。

span 的缓存标识为空/null 是显式 cold miss，未缓存 token 数等于完整本地长度。
非空标识必须有可见证明；找不到、过期、身份或 namespace 不匹配均不能被解释为
cold miss。claim/1（所有 proof 文件均用此结构）字段：claim_id、run_id、request_id、
attempt、revision、tokenizer、prefix（整数 token ID 数组）、matched_tokens、
created_at_us、expires_at_us、available_at_us。created ≤ span start ≤ expires，
prefix 非空且是本地 prompt token 前缀，0 ≤ matched_tokens ≤ prefix 长度 ≤ prompt
长度。按 claim ID 的重复/冲突规则同 span。未缓存工作量 = 本地长度 - matched。
可用于训练的最早时刻是测量、运行、span 和已引用 proof 可获知时刻的最大值。
测量已可见但其引用证据尚不可用时记 excluded，后续 build 可以重新获得资格。

summary-latest.json 是旧 notebook 的成功请求汇总。它的文件生成时间、样本口径
和是否使用服务 span 是不同概念。历史 helper 用于复现 notebook；该汇总不是
本次服务画像的权威来源。
