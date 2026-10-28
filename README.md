# 记忆与人格演化 · memory_evo

> 一个不做向量库、不接云服务、只记「值得记的」的 AstrBot 长期记忆插件。
> 同时让角色的性格在几十次对话里缓慢漂移，而不是每轮重开存档。

![version](https://img.shields.io/badge/version-v0.1.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.26-7c3aed)
![deps](https://img.shields.io/badge/dependencies-0-brightgreen)
![tests](https://img.shields.io/badge/tests-21%20passed-success)

**一句话**：把聊天压成结构化事实存进 SQLite，每轮按相关度取几条注入；把性格拆成五个连续维度，每次演化限速、并且持续向基线回归。

---

## 为什么还要再造一个

AstrBot 已经有两套记忆相关的能力，但它们各自差一块：

| 现有能力 | 能做什么 | 差在哪 |
|---|---|---|
| 上下文压缩（`llm_compress`） | 长对话不被窗口截断 | 摘要是有损的，用户的偏好会被压成「聊过一些饮食话题」 |
| 知识库（RAG） | 手册、FAQ 这类稳态文档 | 不管「这个人是谁」，没有 per-user 概念 |
| 市场记忆插件 | 多是「把最近 N 条塞进 prompt」或外接云服务 | token 爆炸、数据出境，或根本不改人格 |
| 市场人格插件 | 拟人化状态、好感度 | 只变语气不记事实，和记忆插件叠加会互相污染上下文 |

这个插件只补那两块缺口：**关于人的事实** + **会缓慢变化的性格**。其余的交还给框架。

---

## 特性

- **零依赖**：标准库 + SQLite（WAL），没有向量库、没有 embedding、没有外部服务
- **事实级记忆**：按 `(主体, 谓词)` 去重合并；用户改主意时旧值保留为历史，不丢失
- **预算硬约束**：注入前按字符预算截断，默认 600 字符（≈400 token），不会挤爆上下文
- **不破坏提示词缓存**：动态内容走 `extra_user_content_parts`，`system_prompt` 保持稳定
- **会话隔离**：群聊、私聊各记各的，A 群不会串到 B 群
- **人格限速演化**：单次变化 ≤ 6%，每轮向基线回归 1%，永远落在 `[0,1]`
- **可观测、可撤销**：`/memory dump|why|forget|persona`，每次演化都有理由和版本号
- **模型可用工具**：`memory_search` / `memory_write` / `memory_forget`，需要时自己查
- **离线可跑测试**：21 个用例不需要安装 AstrBot 也能执行

---

## 工作原理

### 总览

```mermaid
flowchart LR
    U["用户消息"] --> R["on_llm_request"]
    R --> Q{"召回 top-k"}
    Q --> M["memory 事实块"]
    Q --> P["self_state 人格块"]
    M --> INJ["extra_user_content_parts"]
    P --> INJ
    INJ --> LLM["模型"]
    LLM --> RES["on_llm_response"]
    RES --> BUF[("按会话缓冲 deque")]
    BUF -->|"轮数达标 / 空闲 60s"| EX["LLM 抽取"]
    EX --> FS[("facts 表")]
    EX --> PS[("persona + persona_log")]
    FS --> Q
    PS --> P
```

两条独立的链路：**读链路每轮都跑，写链路由节流触发**。写永远不阻塞回复。

### 一、写：把对话压成事实

```mermaid
flowchart TD
    A["缓冲：user / assistant 交替"] --> B["组装 transcript<br/>尾部截断 4000 字符"]
    B --> C["抽取模型<br/>返回 JSON facts"]
    C --> D{"按 subject + predicate<br/>查找有效记录"}
    D -->|"值相同"| E["置信度 +0.05<br/>更新 updated_at"]
    D -->|"值不同"| F["插入新记录"]
    F --> G["旧记录 superseded_by = 新 id"]
    D -->|"不存在"| H["插入新记录"]
```

三个刻意的设计：

1. **抽取而不是存档**。原文太长、噪声太多，而且「用户说过 300 句话」本身不可检索。抽成 `{subject, predicate, object}` 之后，一条记忆就是一个可打分、可比较、可覆盖的单元。
2. **superseded 而不是 delete**。用户说「我搬去杭州了」不该抹掉「之前住上海」——历史留着，当前值只有一条。`/memory` 只显示有效记录，`history_of()` 能看到完整变迁。
3. **写操作异步**。抽取要额外调一次 LLM，放在回复链路里会拖慢首 token。所以先缓冲，等轮数达标或空闲 60 秒再批量处理；失败只记日志，不影响本轮回复。

### 二、读：打分 + 预算

召回不用向量，用四个信号线性组合：

```
score = 0.50 × 字面相关     # CJK 单字+双字、拉丁按词的重叠率
      + 0.20 × 新近度       # 0.5 ^ (天数 / 7)，半衰期 7 天
      + 0.20 × 重要度       # importance / 5
      + 0.10 × 置信度       # 抽取时的 confidence
```

按分数降序取，边取边累加字符数，超预算就跳过继续找下一条（而不是直接截断，避免大条目吃光预算）。

注入位置是这个插件唯一「有主见」的地方：

```python
req.extra_user_content_parts.append(TextPart(text="<memory>…</memory>"))
```

而不是 `req.system_prompt += "…"`。原因很实在：system_prompt 每轮都变，模型服务端的提示词缓存就失效，成本和首 token 延迟一起涨。而 `extra_user_content_parts` 落在本轮用户消息之后，既不会因为上下文压缩被丢掉（压缩只从最旧的整轮开始丢，最后一轮永远保留），也不会污染缓存前缀。

### 三、人格：为什么要限速

性格不是数值越大越好，而是**不能突变、也不能漂到极值**。所以三层约束叠在一起：

```python
# 1. 步长截断：一次对话最多变 6%
delta = clamp(delta, -max_step, +max_step)

# 2. 基线回归：每轮向出厂性格拉回 decay
value += (baseline - value) * decay

# 3. 硬边界
value = clamp(value, 0.0, 1.0)
```

五个维度：**亲密度 / 跳脱度 / 直给度 / 主动度 / 温度**。

- 只有 `delta`，没有绝对值——模型没法一把把性格设成 1.0
- `decay` 让角色有「性格底色」：吵完架会降温，但不会永久变成另一个人
- 每次演化写入 `persona_log`（traits + 理由 + 版本号），`/memory persona` 可查、可回滚

渲染时不暴露数字，只给程度词：

```
<self_state>
当前状态：亲密度偏高、跳脱度中等、直给度中等、主动度偏低、温度偏高。此刻心情：放松。
自然地体现在语气和取舍里，不要直白复述这些设定。
</self_state>
```

最后那句「不要直白复述」是必须的，否则模型会开始念设定。

### 数据模型

```sql
facts(id, scope, subject, predicate, object, kind, importance,
      confidence, evidence, created_at, updated_at,
      superseded_by, last_hit_at, hits)

persona(scope PK, traits JSON, mood, version, updated_at)

persona_log(id, scope, traits JSON, reason, created_at)
```

`scope` 就是 `unified_msg_origin`（`platform:type:session_id`），天然完成隔离，不需要额外租户逻辑。

---

## 安装

**方式一：WebUI**

插件 → 安装插件 → 填仓库地址 → 确认。

**方式二：手动**

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/1244p/astrbot_plugin_memory_evo.git
# 重启 AstrBot，或在 WebUI 里「重载插件」
```

装完私聊发 `/memory` 看命令是否生效。

数据落在 `data/plugin_data/astrbot_plugin_memory_evo/memory.db`，单文件，删掉即重置。

---

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关 |
| `top_k` | `6` | 每轮注入条数，4~8 比较稳 |
| `max_inject_chars` | `600` | 注入字符上限，别超过上下文窗口 10% |
| `persona_inject` | `true` | 注入 `<self_state>` |
| `persona_evolve` | `true` | 允许人格演化 |
| `max_step` | `0.06` | 单次演化最大步长 |
| `decay` | `0.01` | 向基线回归速度 |
| `baseline_*` | `0.5` | 出厂性格（五维各一项） |
| `flush_turns` | `6` | 多少轮用户消息触发一次抽取 |
| `idle_seconds` | `60` | 空闲多久补写缓冲 |
| `max_buffer` | `40` | 单会话缓冲上限 |
| `extract_provider_id` | 空 | **建议填便宜模型**，留空复用当前会话模型 |
| `llm_timeout` | `45` | 抽取超时，超时跳过不影响回复 |

### 三套现成配方

| 目标 | 配置 |
|---|---|
| 日常陪伴（默认） | `top_k=6, flush_turns=6, max_step=0.05, decay=0.01` |
| 群聊、人多 | `top_k=4, flush_turns=8, max_inject_chars=400` |
| 想要「半年后判若两人」 | `max_step=0.08, decay=0.002`，`baseline_*` 按角色设定 |

---

## 命令

| 命令 | 作用 |
|---|---|
| `/memory` | 帮助 |
| `/memory dump` | 列出当前会话的有效记忆 |
| `/memory why <词>` | 查看召回打分，调 `top_k` 时必备 |
| `/memory forget <id\|词\|all>` | 删除记忆 |
| `/memory persona` | 五维当前值 + 最近 5 次演化及理由 |

## 给模型用的工具

| 工具 | 签名 | 何时调用 |
|---|---|---|
| `memory_search` | `(query, k)` | 回答涉及用户偏好、经历、约定时 |
| `memory_write` | `(content, kind, importance)` | 用户明确说「记住」或出现值得长期保留的信息 |
| `memory_forget` | `(fact_id)` | 用户要求删除某条记忆 |

---

## 与框架其他能力的关系

- **上下文压缩**：不冲突。压缩作用于 `req.contexts`，本插件注入的块在本轮消息里，不会被丢。
- **知识库**：互补。知识库放「文档」，本插件放「关于这个人」。
- **`provider_ltm_settings.group_icl_enable`**：可以共存，但注意预算——两者都在加内容，建议总注入控制在窗口 15% 以内。

## 与同类插件对比

| | 本插件 | 纯事实注入类 | 本地向量类 | 云记忆服务 | 拟人人格类 |
|---|---|---|---|---|---|
| 存什么 | 结构化事实 + 五维人格 | 全文 / Markdown | 向量片段 | 云端托管 | 情绪 / 好感度 |
| 依赖 | 无 | 无 | Embedding 模型 + 磁盘 | 外部 API | 无 |
| token 可控性 | 硬预算 | 常全量塞 prompt | 取决于 top-k | 黑盒 | 不涉及 |
| 人格演化 | 有，带限速与回归 | 无 | 少数有 | 少数有 | 有，但无事实记忆 |
| 隐私 | 全本地 | 全本地 | 全本地 | 出境 | 全本地 |

---

## 开发

```bash
# 不需要安装 AstrBot，用 stub 顶掉依赖
python -m unittest discover -s tests -v
```

```
Ran 21 tests — OK
```

覆盖：重复事实合并、改值 supersede、会话隔离、召回排序与预算、遗忘、人格步长/边界/回归、
JSON 容错解析、钩子注入、工具读写、flush 落库。

目录：

```
astrbot_plugin_memory_evo/
├── main.py        # 钩子、工具、命令
├── store.py       # SQLite 事实库与人格状态
├── persona.py     # 五维演化数学（纯函数）
├── extractor.py   # 提示词与容错 JSON 解析
├── _conf_schema.json
├── metadata.yaml
└── tests/
```

## Roadmap

- [ ] 召回可选接入 embedding（只改 `store.recall`，接口不变）
- [ ] 群聊按 `sender_id` 再分一层，支持「群里每个人都记得住」
- [ ] 事实超过阈值时做同谓词合并摘要
- [ ] 从 `task` / `event` 记忆里取到期项，配合 cron 主动提一句
- [ ] WebUI 管理页（浏览、编辑、回滚人格）

## FAQ

**Q：为什么不用向量库？**
单机小规模下，几百到几千条事实的字面匹配 + 新近度已经够用，延迟微秒级、零额外成本。等真到上万条、同义改写开始漏召回，再换 embedding——接口是同一个函数。

**Q：记忆会不会越存越乱？**
不会无限增长：同一个 `(主体, 谓词)` 只有一条有效记录，改主意是 supersede 不是堆叠。真要清理，`/memory forget all` 一条命令。

**Q：群聊里会不会把 A 说的话记成 B 的？**
当前按会话隔离，群内不区分发言人——适合「群作为一个整体」的场景。要分人见 Roadmap 第二项。

**Q：抽取用主模型会不会很贵？**
会。所以配置里有 `extract_provider_id`，填一个便宜模型；`flush_turns` 调大也能直接降频。

**Q：人格会不会被用户故意带偏？**
有 `max_step` 限速和 `decay` 回归，单次对话最多 6%，且每轮往基线拉回。要更保险就把 `max_step` 降到 0.03。

---

## License

[MIT](LICENSE)

## 致谢

思路来自 AstrBot 社区的多次讨论：记忆该存「事实」而不是「文本」，人格该「漂移」而不是「设定」。
