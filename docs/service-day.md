# 服务日解释与版本发布

本文件说明时刻表接入层的业务规则。原始响应如何接收、归档见
`source-protocol.md`；这里描述如何在原始资料之上形成一组「业务只看到」
的相容版本，以及派班记录如何回查当时依据。

## 来源文档

线路时刻表与运营日历是两份独立文档，可以分两次到达，顺序不限。

### 时刻表 `kind=timetable`

| 字段 | 含义 |
| --- | --- |
| `version` | 时刻表自身版本号，原样保留 |
| `calendar_version` | 这份时刻表据以编写的日历版本 |
| `valid_from` / `valid_to` | 时刻表有效期（服务日） |
| `operators.名称.zone` | 运营方所用 IANA 时区 |
| `departures[]` | 班次列表 |

每个 `departures[]` 元素声明 `operator`、`trip`、`stop`、`service_day`、
`departure`，可选 `utc_offset`（如 `+02:00`）。`departure` 是供方原文，
按 `HH:MM` 或扩展小时 `H:MM` 书写：

- `26:30` 表示服务日次日当地 02:30，文档保存原文 `26:30` 与秒数 `95400`，
  **不会**被改写为次日 02:30。
- 同一运营方同一 `trip`/`stop`/`service_day` 重复出现会被拒绝，不做去重合并。
- 时刻表声明的运营方必须在 `operators` 中，班次不得自带与运营方冲突的时区。

### 日历 `kind=calendar`

| 字段 | 含义 |
| --- | --- |
| `version` | 日历版本号 |
| `valid_from` / `valid_to` | 日历覆盖的服务日范围 |
| `operators` | 可选，运营方时区；与时刻表不一致时拒绝配对 |
| `exceptions[]` | 例外停开 |

`exceptions[]` 元素为 `date`、`scope`（可含 `operator`、`trip`）、`reason`。
`scope` 留空表示该服务日全部停开。例外日期必须在日历有效期内。

## 不猜测的本地时间解释

每条班次按其 `service_day`、`operators.zone` 与原文时刻解释，结果 `status`
只有以下几种：

| status | 含义 | candidates |
| --- | --- | --- |
| `unique` | 唯一 UTC 时刻 | 一条 |
| `repeated` | 夏令时回拨，同一当地时间出现两次 | 两条，全部标为 `ambiguous` |
| `non_existent` | 夏令时前拨，该当地时间不存在 | 空 |
| `service_closed` | 日历声明该服务日停开 | 空，并附 `closure` |
| `departure_missing` | 新版本中已无该班次（仅出现在派班绑定中） | 空 |

`repeated` 不会任选一次：两个候选都保留，`chosen` 为 `null`，班次进入歧义清单。
`non_existent` 同样不落任何时刻，不会借系统默认时区补一个值。
供方若在班次上给出 `utc_offset`，则按该偏移选定唯一时刻（`chosen=source_offset`）；
偏移与任何候选都不符时直接报错，不覆盖供方声明。

每次解释都记录所用规则证据：`zone`、`tzdata_version`（如 `2026f`）、
`tzdata_source`、`zone_digest`，以及该民用日附近的时区切换记录。
解释依据来自运行环境的系统时区库；库版本无法识别时记为空，不假定。

## 版本配对

两个原始文档按内容摘要存为 `documents`，相同正文只存一份。只有同时满足以下条件
才发布一个版本（`versions`，状态 `published`，上一版本转为 `superseded`）：

1. 两半都存在（本次 304 / 未变化的一半取当前版本中的那一份）；
2. 日历覆盖时刻表中出现的所有 `service_day`；
3. 两半对同一运营方的时区声明一致；
4. 新时刻表只与它 `calendar_version` 声明的日历配对；声明的日历尚未到达时，
   该时刻表保留为 `pending`（`code=calendar_version_pending`），旧版本继续生效。
   已在生效的时刻表可以与更新的日历配对（日历修订独立演进）。

任一条不满足时发布失败会被记录（`code=coverage_gap` / `zone_conflict` 等），
**当前版本继续生效**，不会出现半新半旧的组合。重复运行 `sync` 不会重复发布。
来源回退到先前文档时复用原版本号重新生效，历史版本与绑定不删除。

## 派班记录与复核

`assign_shift` 把一条派班记录绑定到当前版本，写入一条只追加的绑定快照：
来源时刻原文、服务日、时区、时区规则证据、全部候选时刻、`service_state`
（`operating` / `closed_by_source` / `departure_missing`）。派班记录本身不被
修改或删除。

新版本发布时，所有已绑定的班次按新版本重新解释，并与各自最近一条绑定比较。
受影响的情况会写入复核队列（`review_items`，状态 `open`）：

| reason | 触发条件 |
| --- | --- |
| `time_changed` | 来源时刻原文变化 |
| `source_offset_changed` | 供方给出的偏移变化 |
| `interpretation_changed` | 解释状态变化 |
| `instants_changed` | 候选 UTC 时刻集合变化 |
| `service_closed` | 新日历声明停开 |
| `service_reopened` | 之前停开、现在恢复 |
| `departure_removed` | 新时刻表已无此班次 |
| `departure_restored` | 班次重新出现 |

复核项只交给调度处理：`acknowledge` 仅把复核项标记为已处理，不取消派班、
不改派班记录、不改变绑定。仍有 `repeated` / `non_existent` 的班次通过
`ambiguous_shifts` 单独列出，即使没有新版本到达也会持续可见。

## 补抓、缓存与失败恢复

- 每次抓取（含 304、429、5xx、截断、解码失败）都记一条 `observations`，
  失败从不被当成空文档。
- 429/5xx 按 `Retry-After`（秒数或 HTTP 日期）重试，上限受控；重试耗尽后
  保留状态与元数据，下次运行继续。
- `sync` 先抓两半再配对；已抓成功的半份按摘要复用，续跑只补缺的一半。
- 归档库（`raw.sqlite3`）保存原始字节与响应头；状态库（`state.sqlite3`）
  保存文档、版本、派班与复核。重启后两者都可用，`sync` 从已记录的 ETag
  继续条件请求。

## 回查

`shift_trace(external_id)` 返回该派班记录的全部绑定（按时间顺序），每条含：
绑定时的版本（时刻表/日历版本、摘要）、对应原始抓取的 `capture_id`、
来源时刻原文、时区与规则证据、候选时刻及所选依据。据此可从一条派班记录
一直查到当时收到的原始响应。
