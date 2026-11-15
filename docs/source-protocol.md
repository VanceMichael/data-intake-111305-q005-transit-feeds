# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `operator`、`trip`、`service_day`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

合成场景 `fixtures/winter_switch.json` 演示冬季时刻表切换，时刻表与例外日历分两个路由（`/feed`、`/calendar`）两次到达：

- 跨日时刻：N12 在服务日 2026-10-31 的 `26:30`，墙钟落到次日 02:30，解释为唯一 UTC；业务侧可核对来源写法被逐字保留。
- 回拨歧义：N13 在 2026-10-25 的 `02:30` 落在 Europe/Berlin 夏令时回拨区间，同一当地时间对应两个 UTC，业务侧应看到两个候选与歧义复核。
- 前跳空档：N14 在 2027-03-28 的 `02:30` 是不存在的当地时间，业务侧应看到 `nonexistent` 状态与空档边界而非系统默认值。
- 分版到达：仅抓取 `/feed` 时无有效业务版本；`/calendar` 到达后才按相同 `operator` 与 `calendar_version` 装配为一组相容 bundle。

来源身份字段 `operator`、`trip`、`service_day` 仍是供方自述，不等同于已解决的业务实体；服务日解释规则见 `docs/service-day.md`。

另外提供 `/rate-limited`、`/unstable`、`/range`、`/range-ignored`、`/truncated`、`/observations/1`、`/observations/2`、`/observations/3`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。
