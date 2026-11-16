# 跨时区班次源的服务日校准

这是资料接收程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，
能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。在原始采集之上，它把线路时刻表
与运营日历配成一组相容版本，按服务日解释时刻，并从派班记录保留可回查的依据。
当前不自动执行门户提交；重复观察会分别保留，原始内容按摘要复用。

服务日解释、版本配对、复核队列与回查口径见 `docs/service-day.md`；来源协议见
`docs/source-protocol.md`。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q transit_feeds tests
python3 -m transit_feeds demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m transit_feeds serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m transit_feeds capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释可与原始接收独立演进。

## 时刻表接入、版本与复核

```sh
python3 -m transit_feeds sync --base-url http://127.0.0.1:实际端口 --runtime .runtime
python3 -m transit_feeds assign --runtime .runtime --id SHIFT-D30 --operator border-west --trip D30 --stop central --service-day 2026-10-31
python3 -m transit_feeds review --runtime .runtime
python3 -m transit_feeds trace --runtime .runtime --id SHIFT-D30
python3 -m transit_feeds acknowledge --runtime .runtime --item 1
python3 -m transit_feeds pending --runtime .runtime
```

`sync` 分两次抓取 `/timetable` 与 `/calendar`，两半相容才发布一组业务可见版本；
只到一半时上一版本继续生效，失败按 `Retry-After` 重试并保留状态。`assign` 把派班
记录绑定到当前版本并留下解释快照；`review` 列出日历/时刻表更新后受影响的班次和
仍有时间歧义的班次，`acknowledge` 只标记复核项已处理，不取消或改写派班；
`trace` 从派班记录回查来源时刻、时区规则与候选时刻。

## 开发资料

来源协议见 `docs/source-protocol.md`，服务日规则见 `docs/service-day.md`。领域观察
关系见 `tests/test_source_contract.py`，业务交错情况见 `fixtures/business_cases.json`。
测试验证原始采集、服务日解释、版本配对、复核队列与失败恢复。所有条目、机构、
文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `transit_feeds` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m transit_feeds demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
