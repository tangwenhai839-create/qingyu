# Qingyu（清语）

**一个自托管、只追加的 AI 对话记忆网关，加上一个"出错就拒绝"（fail-closed）的治理核心，用来确定这些记忆属于谁。**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![状态：开发中](https://img.shields.io/badge/status-work%20in%20progress-orange)
[![CI](https://github.com/tangwenhai839-create/qingyu/actions/workflows/ci.yml/badge.svg)](https://github.com/tangwenhai839-create/qingyu/actions/workflows/ci.yml)

[English](README.md)

> [!WARNING]
> **状态：开发中，未完成，不可用于生产。** 这是一个早期、仍在积极开发的个人项目，接口、数据结构和迁移都可能变化。用真实数据之前请先看下面的状态说明。

## 状态：开发中

以下基于本仓库 2026 年 10 月的代码和测试：

**已实现并有测试**

- **Gateway（Phase 3 已冻结 + Amendment #1）**：按 SHA-256 内容寻址的原始归档和清单；ChatGPT 导出导入（zip 上传、暂存、重试、清理 worker）；归一化的对话/消息/附件证据；只读（GET-only）证据查询 API，有独立读令牌和签名分页游标。共 16 个 Alembic 迁移。Gateway 测试（347 通过、6 跳过）在本地 PostgreSQL 16 上通过。
- **Contracts**：`evidence-query-v1` 和 `core-identity-v1` 的 Pydantic DTO 与 JSON Schema，有测试。
- **证据查询客户端**：[`clients/python`](clients/python) 是可安装的 `qingyu-client`，用 httpx 包装只读的 `GET /api/v1/evidence`，复用这些 DTO，并跟随签名分页游标直到 `next_cursor` 为 null。
- **Core 基础部分**：GENESIS 服务（配内存版 2-of-3 测试权威）、continuity/epoch 隔离校验、读模型和 Gateway 证据客户端、规范化 JSON 哈希、审计记录，单元测试可在普通 Linux 上运行（见 `ci/core-portable-tests.txt`）。

**部分完成 / 验收中**

- **Core R1 控制面**（签名操作、协调器、PostgreSQL 持久化、签名者信任、激活生命周期，以及 phase-b2 的激活、签名者准入、备份、迁移准备）：代码大部分已写，但验收没有完成。`core/docs/R1_ACCEPTANCE_LONG_RUN_STATE.md` 记录最小信任模型设计评审为 **BLOCKED**，`core/docs/CURRENT_SCOPE.md` 写明 Phase B、Phase C **未完成**。
- **Core 测试可移植性**：约三分之二的 Core 测试文件能在普通 Linux 上跑，其余需要 Docker 管理的 PostgreSQL 角色、Windows API（`winreg`、WSL 路径）或所有者私有配置，见 [`ci/core-portable-tests.txt`](ci/core-portable-tests.txt)。

**尚未开始（设计文档中明确推迟）**

- 生产环境 GENESIS 与正式激活
- 独立见证服务（IBCS）、多写者共识、拜占庭容错
- 在线密钥轮换、完整抑制 / 加密擦除
- Memory Intelligence（派生记忆）、用户 / 关系模型、Runtime 事件接入、模型提供方集成

## 它是做什么的？

你和 AI 助手的历史散落在各家导出文件、App 和聊天记录里；工具替你保存的"记忆"可能被悄悄改写，换模型就丢，恢复备份时还可能出现两个"自己"。Qingyu 想给一个长期存在的助手身份，提供**可信、自己托管的历史**：

- **对话归档一次，永久保存。** 导入 ChatGPT 数据导出，原始字节按内容寻址保存，解析后的对话成为只追加的证据。
- **别的工具只能读，不能改。** Agent、运行时或记忆引擎通过有独立令牌的只读 API 查询历史。
- **身份与模型分离。** "还是不是同一个助手"由 Core 里的身份记录决定，而不是看当前跑的是哪个模型或 App。

目前适合的人：在做个人 AI 助手或 Agent 运行时、想要一个可审计且自托管的记忆层的开发者，以及对这些设计问题（只追加证据、写隔离、恢复安全）感兴趣的人。它**不是**开箱即用的终端用户应用。

## 设计思路

以下概括自 `core/docs/architecture/QINGYU_CORE_PHASE1_DESIGN_REPORT.md`、`core/docs/CONTROL_PLANE_BOUNDARY.md`、`core/docs/CONTROL_PLANE_R1_SIMPLIFIED_TRUST_MODEL.md` 和 `gateway/docs/`。

- **权威单向流动**：`原始归档 → 归一化证据 → 派生记忆 → 上下文 → 运行时 → 模型`。下游可以删掉重建，但永远不能反过来改写上游。
- **Gateway 与 Core 分开**：Gateway 回答"来源当时记录了什么"，只追加，不依赖 Core、运行时或模型；Core 回答"这些历史属于谁、长期怎么理解"。分开后，上层逻辑出 bug 或被攻破，也污染不了证据。
- **只读证据查询**：Core 等使用方只通过 Gateway 的 GET-only 证据查询 API 读取，只引用证据，不复制成新的真源，也不建跨库外键。
- **Core 有自己的数据库，没有 Gateway 凭据**：Core 不导入 Gateway 的 Python 模块，也拿不到 Gateway 数据库凭据。删掉 Core 不会让 Gateway 历史失效。
- **默认 fail-closed**：控制面权威不可达、状态对不上或缺少证明时，Core 拒绝写入，而不是猜。默认的权威后端就是"不可用"，所以新装好的系统在显式配置之前写不进去。
- **用 epoch 做写隔离**：写入带 `(continuity_id, epoch, lease)`。过期的写者（比如恢复出来的备份、克隆的数据库）会被 `EPOCH_STALE` / `LEASE_FENCED` 拒绝，因为签发 epoch 的权威不在可克隆的 Core 备份里。
- **2-of-3 授权**：权威接口要求 3 个见证中 2 个同意。目前仓库里只有一个用于确定性测试的内存版 2-of-3 实现，生产见证推迟。
- **GENESIS 需要单独授权**：任何迁移都不会创建身份。创建唯一根身份（GENESIS）是一次显式、经授权、由不随备份克隆的控制面中介的操作。
- **可审计**：变更写成只追加的审计记录，新断言取代旧断言而不是覆盖；哈希使用规范化 JSON（RFC 8785），证据可以独立核验。

## 架构

见 [英文 README 的架构图](README.md#architecture)。目录：`gateway/`（记忆网关）、`core/`（身份、治理、审计、R1 控制面）、`contracts/`（共享契约）、`clients/python/`（只读证据查询客户端）。

## 快速开始（Gateway）

从克隆到第一次证据查询的步骤见 [docs/QUICKSTART.md](docs/QUICKSTART.md)（英文）。

需要 Python 3.12 和 PostgreSQL 16。以下步骤已在 Linux + 本地 PostgreSQL 16 上验证；`docker compose` 一步供方便使用。

```bash
git clone https://github.com/tangwenhai839-create/qingyu.git
cd qingyu/gateway
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# 在 .env 中设置 POSTGRES_PASSWORD 以及匹配的 DATABASE_URL，例如：
#   POSTGRES_PASSWORD=<密码>
#   DATABASE_URL=postgresql+psycopg://qingyu:<密码>@127.0.0.1:5432/qingyu
#   如果密码包含 URL 特殊字符（@、:、/、%），请在 DATABASE_URL 中对其做百分号编码，
#   这样连接字符串才能正常解析。
# 以及三个互不相同的随机密钥 API_TOKEN、EVIDENCE_READ_API_TOKEN、EVIDENCE_CURSOR_SIGNING_KEY，
# 和一个可写的 ARCHIVE_ROOT 目录
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 启动 PostgreSQL 16（自动从 .env 读取 POSTGRES_PASSWORD）：
docker compose up -d db

alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

测试方法见英文 README 的 "Running tests"。

## 路线图

与上面的状态一致，不承诺时间：

0. GitHub Actions CI 已启用，工作流在 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)（lint、contracts、证据查询客户端、PostgreSQL 16 上的 Gateway、可移植的 Core 子集）。
1. 让 Core 全部测试能在 Linux CI 上运行。
2. 解决 Core R1 验收阻塞，完成 phase-b2。
3. 生产 GENESIS 与激活流程，配独立见证。
4. 支持 ChatGPT 以外的更多导入来源。
5. Memory Intelligence：基于证据、可重建的派生记忆。

## 参与贡献

欢迎贡献，尤其是文档、测试和跨平台支持。可以从 `good first issue` / `help wanted` 标签的 issue 开始，先看 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [行为准则](CODE_OF_CONDUCT.md)，问题和想法发到 [Discussions](https://github.com/tangwenhai839-create/qingyu/discussions)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 历史与 AI 辅助开发

本仓库由原先各组件仓库合并而来，保留了 2026 年 9 月以来的提交历史和原始日期。公开前已从历史中移除验收证据、数据库快照、备份和本机路径，所以部分文档引用的证据文件不在本仓库中。大量代码由 AI 编程助手（如 OpenAI Codex）在维护者指导和审查下编写，提交身份已统一为维护者账号。

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。
