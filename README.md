# 跨时区班次源的服务日校准

这是资料接收与服务日解释程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。解释层在原始接收之上，把供方时刻结合运营方时区与服务日解释为带依据的 UTC 候选，装配时刻表与例外日历的相容版本，并为调度保留派班追溯与复核队列。原始归档只追加、不可变；重复观察分别保留，原始内容按摘要复用。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库（本机为 tzdata 2026f），解释结果记录所用时区名与 tzdata 版本。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

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

演示从空的临时目录启动本地合成来源，真实完成两次 HTTP 获取（时刻表 `/feed` 与例外日历 `/calendar`）及归档，再完成服务日解释、版本装配与派班复核：`26:30` 给出确定 UTC，夏令时回拨的 `02:30` 保留两个候选，前跳空档的 `02:30` 标记为不存在；结束自动清理。本地测试、编译和演示合计一般不超过十秒，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m transit_feeds serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m transit_feeds capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释与原始接收独立演进：`serviceday.py` 做纯函数式服务日/时区解释，`catalog.py` 装配时刻表与日历的相容版本，`repository.py` 持久化解释缓存、有效版本、派班与复核，`ingestion.py` 编排可重放的摄入事务。规则与口径见 `docs/service-day.md`。

## 服务日命令

```sh
# 只做服务日解释，不落库
python3 -m transit_feeds interpret --records 记录.json
# 摄入记录文件，装配有效业务版本
python3 -m transit_feeds ingest --db .runtime/service.sqlite3 --source local-source --records 记录.json
# 直接从来源摄入（同时写原始归档与解释库）
python3 -m transit_feeds ingest --db .runtime/service.sqlite3 --source local-source \
    --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --path /feed
# 从派班记录查回当时的来源时刻、解释依据与版本历史
python3 -m transit_feeds trace --db .runtime/service.sqlite3 --dispatch-id DISP-N12
# 列出仍有时间歧义或待复核的已派班次
python3 -m transit_feeds reviews --db .runtime/service.sqlite3
```

## 开发资料

来源协议见 `docs/source-protocol.md`，服务日解释、相容版本与失败恢复口径见 `docs/service-day.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径；`fixtures/winter_switch.json` 是冬季换表端到端演练场景。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `transit_feeds` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m transit_feeds demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
