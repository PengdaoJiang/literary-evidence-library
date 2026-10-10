# Literary Evidence Library

新增[增量实体与事实登记库](docs/incremental-registry.md)：保留实际长文本研究中的短事务、任务租约、证据定位、幂等提交和审阅状态实现，附原有合成输入并发测试。它与下方原编译器分别运行，不把集成计划当作完成状态。

从长篇文学资料工程提取的**证据编译器与关系查询工具**。把原文锚点、实体、译名、候选和审定记录编译成可重建 SQLite 与可阅读报告。

关键问题不是“抽出了多少关系”，而是：同名是否同一实体？一次动作是否足以建立长期关系？被拒绝的候选会不会重新混进查询结果？

## 运行

Python 3.10+，仅标准库：

```sh
python -B -m unittest discover -s tests -v
python -B demo.py --out demo-output
python -B demo-output/literary/tools/library.py entity char.lin
python -B demo-output/literary/tools/library.py neighbors char.lin
python -B demo-output/literary/tools/library.py candidates
```

打开 `demo-output/literary/generated/reports/`：实体档案、关系审定表和覆盖报告都由实际编译器生成。样例是本仓库新写的短篇测试文本，审阅记录是**合成的契约测试输入**，不是原项目的文学审定成果或一次真实独立审阅。

## 实现

| 层 | 关键行为 | 源码 |
| --- | --- | --- |
| 原文与锚点 | 字符偏移、精确引文和哈希必须一致 | [`_verify_anchor`](literary/tools/library.py) |
| 身份与关系 | 区分 mention、身份重解析、一次事实与关系时段 | [compiler](literary/tools/library.py) |
| 审定输入 | 拒绝自我批准；候选、反审、修订和采纳有明确对应 | [build_database](literary/tools/library.py) |
| 查询与产物 | 外键、事实视图、可重建索引和来源漂移检查 | [schema](literary/schema/knowledge.sql)、[validator](literary/tools/library.py) |

`neighbors` 和 `relation` 读取已采纳关系；候选只能通过专门入口查看。高置信度不等于事实。这里实现的是确定性证据管理和查询，不声称机器规则能判定文学真伪，也不把关键词查询描述成向量 RAG。

## 工程来源与取舍

核心编译器及完整 SQL 模式来自实际长篇资料工程。公开适配将原先固定的语料规模和中心实体改为显式 `dataset.json`，保留计数、唯一性、证据、来源、身份和审阅链约束。详见 [source provenance](docs/source-provenance.md) 和 [engineering cases](docs/engineering-cases.md)。

开发过程使用 Codex 辅助实现和迭代。原项目小说全文及原文检索库不随代码分发。

## License

[MIT](LICENSE)，涵盖本仓库代码、文档与原创测试样例；不对原项目小说或第三方版本授予权利。
