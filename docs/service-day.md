# 服务日解释设计

面向夜班按运营日结算、跨境班车使用各自运营方时区的场景。本文档说明时刻表接入层如何解释服务日、如何装配相容版本，以及失败恢复与追溯口径。

## 1. 分层与职责

| 模块 | 职责 | 是否可重放 |
| --- | --- | --- |
| `archive.py` | 只追加供方原始响应字节、头部与观察时间，按摘要复用正文 | 是 |
| `decoding.py` | JSON/CSV/门户嵌入块解码为来源记录，不做业务推断 | 是 |
| `serviceday.py` | 纯函数式服务日/时区解释，保留来源写法，输出状态与依据 | 是，无副作用 |
| `catalog.py` | 时刻表部件与日历部件的规范化、相容版本装配、例外应用 | 是 |
| `repository.py` | SQLite 持久化：部件、解释缓存、bundle、实例、派班、复核、运行水位 | 是 |
| `ingestion.py` | 一次摄入的事务编排、失败隔离、版本提升、影响复核 | 是，幂等 |

原始归档库与解释库是两个独立 SQLite 文件：原始字节不可变，解释结果可由原始资料重放重建。

## 2. 时刻解释语义

### 2.1 服务日与跨日小时数

* `service_day` 是运营日方声明的服务归属日，不是自然日零点切分。
* 供方时刻按 `HH:MM[:SS]` 解析，允许小时数 ≥ 24；`26:30` 表示服务日起点之后经过 1590 分钟，墙钟落到次日 `02:30`，并标记 `crosses_midnight=true`。
* 小时数 ≥ 48 判为录入错误（`clock_out_of_range`），不做模 24 回绕。
* `source_departure` 逐字保留供方写法，任何环节都不覆盖。

### 2.2 三种解释状态

| 状态 | 触发条件 | 系统行为 |
| --- | --- | --- |
| `definite` | 唯一成立的 UTC 瞬间（含跨日时刻、无 DST 时区） | 给出单个候选 |
| `ambiguous` | 夏令时回拨，当地时间出现两次（fold=0 与 fold=1 均能 round-trip） | 保留两个 UTC 候选，派班后进入复核 |
| `nonexistent` | 夏令时前跳空档，fold 两种取值都无法 round-trip | 不产生候选，给出空档两侧 `gap_bracket`，进入不可解析复核 |
| `invalid` | 日期/时刻格式错误、小时越界、时区无法解析 | 不产生候选，记录机器可读 `issue_code` |

判定依据 PEP 495：对同一 naive 墙钟分别构造 `fold=0/1`，偏移相同即确定；偏移不同时，两个 fold 都能 round-trip 回原墙钟为歧义，否则为不存在。**任何情况下都不静默回退到系统默认时间或默认 fold。**

### 2.3 时区规则标识与缓存键

每条解释记录 `zone_rule = "<时区名>@tzdata<版本>"`（本机为 `Europe/Berlin@tzdata2026f`）。缓存键是以下依据的 SHA-256：

```
service_day + 原文 departure + zone + calendar_version + zone_rule
```

因此：

* 依据不变的解释跨时刻表版本、跨 bundle 复用同一条 `interpretations` 记录；
* 时区数据库升级后 `zone_rule` 改变，旧缓存不会被误用；
* 日历版本属于依据的一部分，版本变化需要重新解释。

## 3. 相容版本装配

### 3.1 两类部件

* `timetable` 部件：必须声明 `calendar_version`，含 `trips` 数组；
* `calendar` 部件：同 `operator` + 同 `calendar_version`，含 `exceptions`（`cancel` 停开 / `added` 增补）。

部件按规范化 JSON 的摘要去重，只追加；新修订不覆盖旧修订。

### 3.2 装配规则

* 仅当同一 `(operator, calendar_version)` 的时刻表与日历都到达，才组成一组 bundle；
* 单到的部件保持 `pending`，业务有效视图不暴露，也绝不与其他 `calendar_version` 混搭；
* 每个运营方同一时刻只有一组 `is_effective=1` 的 bundle；同批次到达多组新 bundle 时，只提升部件 id 最大（最新到达）的一组，其余实例化留档可追溯；
* 例外命中基础班次则改写 `operates/action` 但保留来源时刻；增补班次产生新实例；无对应基础班次的停开例外产生装配警告，不丢数据。

## 4. 派班、追溯与复核

### 4.1 派班冻结

`record_dispatch` 在派班时把当时的解释 JSON、来源时刻、bundle id 与原始归档 capture id 冻结到 `dispatches`。之后 bundle 升级不会改写历史派班。`trace_dispatch` 返回：

* 冻结的来源时刻与解释依据（含候选 UTC、zone_rule、issue_code）；
* 所属 bundle 与该班次在所有历史版本上的解释；
* 关联复核条目及其处理结论。

### 4.2 日历更新影响分析

新 bundle 提升前逐班比较新旧实例，差异分三类：`removed`（新版消失）、`added`（新版新增）、`changed`（运营状态/时刻/时区解释变化）。仅对其中**已存在派班记录**的班次插入 `calendar_change` 复核，按 `dispatch+班次+原因` 去重。系统不取消、不改派任何任务，处置权在调度。

### 4.3 仍有时间歧义的班次

`list_ambiguous_dispatches` 汇总所有未解决的时间问题：

* `ambiguous_dispatch`：回拨歧义，候选 UTC 未确认；
* `unresolvable_dispatch`：不存在的当地时间或无法解析的写法。

调度在复核队列中确认或回填依据后调用 `resolve_review` 关闭条目。

## 5. 补抓、缓存与失败恢复

* **条件请求**：304 不产生新正文，不触发摄入；水位（ETag、最后 capture id）记在 `source_watermarks`。
* **补抓幂等**：重复内容因部件摘要唯一约束被跳过，`parts_new=0`、无新 bundle、无新解释。
* **事务边界**：一次摄入登记 `ingestion_runs`（running/done/failed）；实例化在单事务内整体替换，失败回滚，不残留半成品 bundle。
* **坏数据隔离**：规范化失败的记录写入 `quarantine` 并附原因，不中断同批其他部件。
* **重放能力**：解释库可由原始归档库重新摄入完整重建；解释缓存命中时结果与首次计算字节一致。

## 6. 命令行

```sh
python3 -m transit_feeds interpret --records 记录.json      # 只解释不落库
python3 -m transit_feeds ingest --db svc.sqlite3 --source op --records 记录.json
python3 -m transit_feeds ingest --db svc.sqlite3 --source op \
    --base-url http://127.0.0.1:端口 --archive raw.sqlite3 --path /feed
python3 -m transit_feeds trace --db svc.sqlite3 --dispatch-id DISP-N12
python3 -m transit_feeds reviews --db svc.sqlite3
python3 -m transit_feeds demo                                 # 本地端到端演练
```

所有资料均为本地合成数据，不连接真实门户。
