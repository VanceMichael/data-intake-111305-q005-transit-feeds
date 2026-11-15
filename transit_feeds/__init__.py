"""来源采集、原始响应归档与服务日解释。

分层：archive/decoding/client/origin 负责不可变的原始接收；
serviceday 负责服务日与时区解释；catalog 负责相容版本装配；
repository 负责持久化、跨版本缓存、派班追溯与复核；ingestion 负责事务编排。
"""
