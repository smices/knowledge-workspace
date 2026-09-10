# RAG 评测说明

## 数据能力

仓库在 `eval/corpus/` 中保留可复现的 Project Gutenberg 公开测试文本、来源与 SHA-256 清单。测试资料初始化为独立的“测试资料”知识库，覆盖四部文本：

- 《三国演义》：人物关系、主从关系、战役分工。
- 《西游记》：师徒关系、敌对关系、事件原因、团队角色。
- 《水浒传》：陷害与发配、梁山角色、招安动机。
- 《红楼梦》：人物关系、家族关系、事件原因。

每个样本要求：

1. 检索结果命中期望文档。
2. 问答返回至少一个 citation。
3. 结论包含至少一个期望关键词。
4. 关系图/归纳问题不返回无界长列表。
5. 证据不足时明确拒答，不编造。
6. 已批准别名必须命中标准名与别名共同出现的同一版本证据，并返回 `entity_bindings`；候选、拒绝和其他文档版本的别名不得参与命中。
7. 关系型反例中，父子、共同认识或同一事件等间接线索不得被改写为被问实体对的直接关系；证据契约必须标为 `indirect` 或 `insufficient`。

样本可声明 `expected_answer_state: no_answer`。这类样本仍要求检索命中预期文档，但只有返回 `no_answer` 且所有结论均为 `insufficient` 才通过；它用于防止以检索到相关文本为由编造关系。

## 效率能力

默认基准：

- 检索当前题库的串行请求：记录 p50/p95、错误率。
- 问答当前题库的串行请求：记录 p50/p95、错误率。
- 检索并发 4：记录吞吐、错误率和 p95。

第一阶段门槛：

- 检索成功率 ≥ 95%。
- 问答成功率 ≥ 95%。
- 检索和问答错误率 ≤ 5%（与对应成功率互相校验）。
- 检索 p95 ≤ 2 秒（本地 Ollama 基线）。
- 问答 p95 ≤ 60 秒（本地 qwen3:8b 基线）。
- 文档命中率 ≥ 90%。
- 问答文档命中率 ≥ 90%，每个有 `expected_keywords` 的问答样本至少命中一个关键词。
- Retrieval MRR ≥ 0.75，NDCG ≥ 0.75。
- Citation 覆盖率 = 100%。
- 证据契约中直接支持结论比例 ≥ 85%。
- 人工复核全部为 `reviewed`；评测脚本不会自动修改 `eval/review.jsonl`。

每次 `benchmark.py` 运行都会生成新的 `run_id`，并为每个问答结果生成当前证据的 `evidence_hash`。人工复核记录必须包含相同 `run_id`，或包含对应问答项的 `evidence_hash`；只有题目 ID 相同不能通过复核门槛。旧运行的复核不得复制到新结果。

运行：

```bash
source .venv/bin/activate
python eval/init_corpus.py --base-url http://127.0.0.1:8000
python eval/benchmark.py --base-url http://127.0.0.1:18024 --check
# --check 同时执行上述延迟、错误率、质量和人工复核门槛
```

初始化接口仅用于已显式启用 `AUTH_MODE=dev` 的本地评测环境。生产环境不会自动导入测试资料。

报告输出：

```text
eval/results.json
eval/report.md
```

仓库中的现有报告是扩充《红楼梦》用例前的 12 题历史基线；运行当前 16 题评测后会覆盖为最新结果。

评测将检索与生成分开：检索报告文档命中、MRR、NDCG；生成报告 citation 覆盖和证据契约的直接支持率。关系型结论还会校验被问实体对是否在同一原文分句共同出现；生产变更前仍应抽样人工复核高风险结论。

## 容量证据

容量探针只调用检索和问答读取接口，不上传、删除或修改资料。它要求显式提供服务地址、employee session cookie 和 machine Bearer token，限制并发、排队长度、请求超时、响应体大小和持续时间，并分别报告 employee（问答）与 machine（检索）流量，以及 warm/varied 查询、请求时间、排队等待、p50/p95/p99、错误率、缓存命中观测和 HTTP 429。

employee 请求只发送 `Cookie`，machine 请求只发送 `Authorization: Bearer`；探针禁止跟随重定向，输出不会打印任何凭据。`varied` 只是轮换查询，不宣称绕过服务端缓存；只有响应显式返回缓存命中信息时才统计 `cache_hit_rate`。

不要在单元测试中运行真实探针。下面是一次明确的、会产生真实请求并可能消耗 embedding/chat 模型额度的容量测量；`employee-share` 越高，问答模型调用越多：

```bash
RAG_BASE_URL=https://rag.example.test \
RAG_EMPLOYEE_SESSION_COOKIE="$RAG_EMPLOYEE_SESSION_COOKIE" \
RAG_MACHINE_BEARER_TOKEN="$RAG_MACHINE_BEARER_TOKEN" \
python eval/load.py --duration 60 --rate 2 --concurrency 4 --queue-size 8 --employee-share 0.8
```

输出只包含实际测量值和非敏感配置，不包含 token、机器路径或容量承诺。没有代表性的持续负载、错误预算和多轮结果前，不得据此声称支持固定员工或 AI worker 数量。
