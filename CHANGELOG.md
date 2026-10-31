# Changelog

本插件的所有重要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 这一份也供 AstrBot Dashboard 的「更新日志」页读取（框架会找 `CHANGELOG.md` / `changelog.md` / `CHANGELOG` / `changelog`，找不到就打 WARN）。

## 版本节奏

先说清楚规则，免得版本号通胀（v0.1.0 → v0.6.0 只花了 27 小时，9 个 tag，这是把「一次提交」当成了「一个版本」）：

- **Minor（0.x.0）**：只有用户能感知到的能力变化，或**需要处理的升级动作**（配置项改名、库表结构变化）才升。
- **Patch（0.x.y）**：小改动都走这里——修 bug、补文档、改提示词、界面与文案调整，不改用法。
  这类改动**该发就发**，攒着不发的成本比多发一个 tag 更高（用户看不出改没改）。
- **一个 patch 可以含多次提交**：同一件事改到自洽了再发，不必每敲一次键盘就一个 tag。
- **1.0.0**：等市场可见、配置项与库表结构稳定之后再说，不拿它当「功能最全」的奖励。

已经发出去的 tag 不回收——有人可能 pin 了版本、`CHANGELOG` 里也引用了它们，改写历史只会让后来者对不上账。

## [Unreleased]

### Planned

- 按周几区分的允许时段（如 `mon-fri:09:00-12:00`）
- `task` / `event` 到期提醒
- 跨平台身份归一 `/memory link`
- 同槽位合并摘要

## [v0.7.0] - 2026-10-31

### Added

- **插件自己的事件日志**：`plugin_data/astrbot_plugin_memory_evo/logs/memory_evo-YYYYMMDD.log`，
  JSONL、按天分文件、按 `log_days` 自动清理。每条带 `evt` 事件名与结构化字段，不再只能靠
  AstrBot 那几万行输出里翻

  已埋点的事件：

  | 事件 | 什么时候记 | 关键字段 |
  |---|---|---|
  | `plugin.start` / `plugin.stop` | 加载、卸载 | `db`、`enable`、`care`、`log_level` |
  | `janitor.start` / `janitor.fail` | 心跳启动；某一轮抛异常 | `interval` |
  | `flush.begin` / `flush.ok` / `flush.empty` | 每次抽取 | `reason`、`turns`、`facts` |
  | `flush.timeout` / `flush.fail` | 抽取超时或报错 | `reason`、错误消息 |
  | `llm.ok` / `llm.timeout` / `llm.fail` | 模型调用（`llm.ok` 为 DEBUG） | `ms`、`chars`、`parsed` |
  | `provider.none` | 找不到可用模型 | — |
  | `recall.fail` | 召回抛异常 | — |
  | `persona.evolve` | 人格有实际变化 | `deltas` |
  | `care.sent` / `care.skip` | 主动消息发了；原因变了才记一条 | `reason` |
  | `care.fail` / `care.send_fail` | 判定或发送失败 | — |
  | `settings.save` | 面板保存设置 | `changed` |
  | `logs.clear` | 清空日志 | `files` |

- 面板新增「日志」页签：按级别筛（全部 / DEBUG / INFO / WARN / ERROR）、关键词搜（事件名、消息、
  字段值一起搜）、刷新、清空，顶部一条显示当天的级别计数与高频事件
- `/memory logs [n]` 与 `/memory logs clear`，不用开面板也能看最近发生了什么
- 新配置：`log_level`（默认 INFO）、`log_days`（默认 7）、`log_mirror`（WARN/ERROR 同时写 AstrBot 日志，默认开）

### Changed

- `settings.py` 的分组多了「日志」一组；面板设置页按同样的顺序渲染

测试：142 → 166 个用例（`tests/test_plog.py` 19 条 + 插件集成 5 条）。

## [v0.6.2] - 2026-10-31

### Fixed

- 后台心跳（每 20s 的空闲落盘 + 向量补齐，每 60s 的主动消息检查）原来把 `while True` 包在一层 `try` 里，
  任意一轮抛异常循环就永久退出，之后「记忆停在昨天、主动消息再也不发」，日志里只有一行容易被刷掉的 exception。
  现在每轮单独兜异常，出错只跳过这一轮
- 主动消息不发时看不出原因（`decide()` 的结果只写 DEBUG 日志）：面板「主动消息」页新增「上次检查 X → 原因」，
  后端每轮把判定结果记在内存里供面板与命令读取
- 进程重启后会话集合是空的，要等用户先说一句话主动消息才会重新开始检查：`store.known_scopes()` 在上电时从库里恢复

### Added

- `/memory care why`：逐条列出当前闸门（空闲、间隔、今日额度、未回应、免打扰、允许时段、开关）与结论，
  外加「上次自动检查」时间，用来区分「闸门没过」和「心跳根本没跑」
- `proactive.REASON_TEXT` / `reason_text()`：判定原因统一成一句中文，面板、日志、命令共用一份
- `dashboard.care_block()` 增加 `checked_at` / `check_reason` / `check_text`

测试：127 → 142 个用例。

## [v0.6.1] - 2026-10-30

### Changed

- 面板视觉重做：配色与间距改用 Dashboard 自身主题（`#3c96ca` / `#1b1c1d` / `#d0d0d0`），深色模式跟 `data-theme` 自动切换
- 记忆从表格改成列表行：槽位芯片 + 正文 + 一行元信息（类型 / 重要度 / 置信度 / 召回次数 / 更新时间 / id），
  不再出现「内容」列被右侧数字挤窄、时间被截断成「上午2:0」
- 指标从六张等宽卡片改成一条分隔指标带，只有「待确认」在大于 0 时高亮，不再整张卡片报警
- 页签改成分段控件；搜索改成输入即搜（防抖 260ms）；类型/状态改用芯片，选中态明确
- 批量操作改成选中后浮出的浮动操作条，不再让三个按钮常驻工具条
- 人格页：五张 trait 卡带基线刻度与偏离值，演化记录改成竖向时间线并显示每次的维度变化量
- 主动消息页：顶部状态卡（可发/等待变色）+ 判定闸门网格 + 上次发言引用 + 开关组件
- 设置页：字段卡片网格、按名称过滤配置项、字段级错误标红、底部粘性保存条显示「已改 N 项，待保存」
- `care` 路由的 `settings` 增加 `max_chars`（面板展示单条字数上限）
- 新增 `scripts/preview_server.py`（开发用，不进插件运行路径）：本地起一个带假桥接的预览页，方便截图调样式
- README 补界面截图

## [v0.6.0] - 2026-10-29

### Added

- 面板新增「设置」页签：37 个配置项按 基础 / 注入与召回 / 人格 / 语义召回 / 主动消息 / 抽取模型 六组渲染，
  不用再回 Dashboard 的插件配置页翻 JSON
- 后端 `settings` 路由（`GET` 读、`POST` 写），表单描述由 `_conf_schema.json` 生成，新增配置项不会漏显示
- 保存前做整体校验：数值边界（如 `top_k` 1–20、`max_step` 0–0.2）、`HH:MM` 时间、允许时段格式、时区名；
  任一字段不合法就整体不落盘，前端按字段标红
- 保存即生效：改动写回 `AstrBotConfig` 并 `save_config()`，钩子下一轮就读到新值；改 `baseline_*` 会重算内存基线，
  改 `embedding_provider_id` 会清嵌入缓存
- `db_path` 标「需重启」，其余字段改完即时可用

### Changed

- 插件内路由从 8 条增到 9 条（多一条 `settings`）
- 测试 105 → 127

## [v0.5.1] - 2026-10-29

### Fixed

- 面板「下次最早」一直显示 `—`：原先只回显库里存的 `next_gap_minutes`，而这一列只有在**主动发过**或
  **巡检被跳过**时才写入，新装/刚清库的会话永远是 0
- 现在由后端算出实际时刻：`next_at = max(last_sent + next_gap, last_active + care_min_idle_minutes)`，
  并带上 `next_reason`（`never_sent` / `gap` / `idle`）与 `ready`（闸门是否已过），面板显示成 `HH:MM（N 分钟后）`
  或「现在就可以」

### Changed

- `dashboard.care_block()` 增加 `min_idle_minutes` 参数，返回 `idle_until` / `gap_until` / `next_at` / `next_reason` / `ready`
- 主动消息页签多一行「判定依据」，说明当前卡在哪道闸门

### Tests

- 100 → 105：从未发过、等间隔、等空闲、空闲已够、两道闸门取更晚者

## [v0.5.0] - 2026-10-29

### Added

- **WebUI 面板**（`views/memory/`）：在 AstrBot Dashboard → 插件 → 详情页里打开，三个页签
  - 记忆：搜索 / 按类型与状态筛选、分页、勾选批量删除、批量确认待确认条目、导出该会话 JSON
  - 人格：五维当前值（带基线刻度）+ 演化时间线（版本号、原因、当时各维取值）
  - 主动消息：本会话开关、今日已发/上限、连续未回应、下次最早间隔、免打扰与允许时段
- 后端只暴露 8 个插件内路由：`scopes` / `overview` / `facts` / `facts/delete` / `facts/confirm` / `persona` / `care` / `export`，
  全部通过 `context.register_web_api()` 注册，走 Dashboard 的插件转发（`/api/v1/plugins/extensions/<plugin>/...`）
- 面板语言与主题跟随 Dashboard（`bridge.ready()` 的 `isDark` / `locale`），零前端依赖，单文件 `app.js`

### Security / 边界

- 面板只操作**当前选中的会话**：删除与确认都带 `scope` 条件，跨会话 id 传进来也删不动（有测试）
- `confirm` 只认 `status = 'suspect'`，不会把已取代/已生效的条目顺手改写
- 面板不是常开服务：路由仅在插件加载时注册，不额外监听端口

### Compatibility

- 老版本 AstrBot 没有 `register_web_api` 时自动跳过注册，插件其余功能照常；面板需要 AstrBot ≥ 4.29
- 面板读取的是同一个 `memory.db`，与 `/memory` 命令看到的数据一致

### Tests

- 75 → 100：视图注册（路由前缀、方法、无 `register_web_api` 的降级）、静态资源存在、概览统计、筛选分页、
  跨会话删除隔离、`confirm` 只动 suspect、人格与主动消息区块、导出 JSON 可序列化

## [v0.4.1] - 2026-10-29

### Added

- **允许时段白名单** `care_schedule`：`"09:00-12:00,19:00-23:00"`，留空即不限时段，老配置行为不变
- 支持跨零点窗口（`22:00-02:00`）与中文逗号分隔
- `/memory care status` 增加「允许时段」一行

### Fixed

- 时段解析不再把越界值 clamp 成 `23:59`（`25:99-26:00` 原先会变成反向窗口，等于全天可发），
  改用严格的 `_strict_hhmm`，非法片段直接丢弃
- 免打扰（黑名单）与允许时段（白名单）冲突时明确黑名单优先；`/memory care now` 强制发送同样守这两道

### Tests

- 64 → 75：解析容错、中文逗号、端点左闭右开、跨零点、黑名单优先、force 仍守时段

## [v0.4.0] - 2026-10-29

### Added

- **主动开口**：用户安静够久时自己发一条，只对私聊会话生效
- 四道闸门：免打扰 `23:30–08:00`、每日上限 3 条、连续 2 次未回应即停、触发概率 0.85
- 模型自决节奏：生成时返回 `next_gap_minutes`（15–240），下次巡检至少等这么久
- 内容来自人格状态 + 记忆 top4 + 上次说过的话（防重复），非模板文案
- `/memory care on|off|status|now`；会话级静音写入数据库，重启保持
- 新增 `proactive.py`、`proactive` 表；配置项 `care_*` 十项，默认**关闭**

### Tests

- 61 → 64：决策矩阵、免打扰边界、会话静音、强制发送、模型选择沉默、真实发送链路

## [v0.3.1] - 2026-10-29

### Fixed

- `extract_provider_id` / `embedding_provider_id` 填了不存在的 ID 时报出框架 WARN
  `Provider xxx was not found.`，且不知道回退到了哪。现在插件自己先检查并告警一次：
  `[memory_evo] extract_provider_id='x' 不存在，已退回当前会话模型。可用 ID：...`
- 同一 provider 只告警一次，不再每个 flush 刷两条

### Added

- `scripts/health_report.py`：只读打开 `memory.db` 输出规模、类型分布、槽位 top8、
  向量覆盖率、平均置信度、从未召回条数、未裁决冲突、人格版本与最近演化

## [v0.3.0] - 2026-10-29

### Added

- **语义召回**：向量 L2 归一化后以 float32 存入 `facts.embedding`，点积即余弦，无需 numpy
- `embedding_model` 不匹配时该条向量自动失效，不会拿错模型的向量算相似度
- 后台每 20 秒为每个会话补写最多 16 条缺失向量；无 embedding provider 时静默回退字面重叠打分
- `/memory reindex` 手动补写

### Changed

- 召回公式 `0.5*cos + 0.2*recency + 0.2*importance/5 + 0.1*confidence`，无向量时第一项换成字面重叠

## [v0.2.0] - 2026-10-29

### Added

- **置信度门控**：`confidence < 0.45` 且 `importance < 5` 的事实不注入
- **矛盾检测**：谓词经 `canonical_predicate` 归一（饮食偏好/忌口/口味 → 饮食），
  极性相反且核心词相同 → 两条都标 `suspect`，用 `/memory confirm <id>` 裁决
- **分类半衰期**：preference 30d / profile 60d / fact 14d / event 7d / task 1d
- **注入多样性**：同槽位最多 2 条、单条最多 80 字

### Tests

- 21 → 30

## [v0.1.0] - 2026-10-29

### Added

- 首版：SQLite 事实库（`facts` 表，含 supersede 链）、五维人格演化、LLM 抽取（每 6 轮或空闲 60 秒触发）
- 注入走 `req.extra_user_content_parts`，绝不改 `req.system_prompt`（避免破坏提示词缓存）
- `/memory dump|why|forget|persona`，工具 `memory_search` / `memory_write` / `memory_forget`
- 零第三方依赖，纯标准库；MIT 许可；GitHub Actions 在 Python 3.10 / 3.12 跑测试

[Unreleased]: https://github.com/1244p/astrbot_plugin_memory_evo/compare/v0.4.1...HEAD
[v0.4.1]: https://github.com/1244p/astrbot_plugin_memory_evo/compare/v0.4.0...v0.4.1
[v0.4.0]: https://github.com/1244p/astrbot_plugin_memory_evo/compare/v0.3.1...v0.4.0
[v0.3.1]: https://github.com/1244p/astrbot_plugin_memory_evo/compare/v0.3.0...v0.3.1
[v0.3.0]: https://github.com/1244p/astrbot_plugin_memory_evo/compare/v0.2.0...v0.3.0
[v0.2.0]: https://github.com/1244p/astrbot_plugin_memory_evo/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/1244p/astrbot_plugin_memory_evo/releases/tag/v0.1.0
