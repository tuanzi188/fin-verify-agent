# KnowledgeMind 智知 — 可扩展的 RAG 智能知识库应用原型

基于 **DeepSeek** 大模型的检索增强生成（RAG）知识库应用，支持多格式文档解析、混合检索、流式对话和多轮会话记忆，适合学习、原型验证和小规模内部使用。

---

## 项目背景

个人开发者和小型团队在学习 RAG、验证架构和搭建内部工具时，常需要处理产品手册、制度文件、会议纪要、技术文档等资料，传统基于关键词的全文搜索难以精准获取所需知识。KnowledgeMind 通过 **RAG（检索增强生成）** 技术，将文档向量化存储，结合 LLM 的语义理解与生成能力，面向学习、原型验证和小规模内部使用的知识库应用。

---

## 项目定位与边界

KnowledgeMind 当前定位为可扩展的 RAG 智能知识库应用原型，主要用于学习、架构验证和小规模内部使用。

当前版本已经包含文档解析、混合检索、流式对话、会话记忆、基础身份认证、会话隔离、Docker Compose 配置和自动化测试。

当前版本尚未完成大规模并发、生产级监控、模型服务高可用、完整运维体系、企业单点登录和正式的多租户管理。因此，部署到真实生产环境前仍需要根据实际场景补充压测、监控、备份、权限管理和故障恢复方案。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **多格式文档解析** | 支持 12 种格式：PDF / Word / Excel / PPT / Markdown / TXT / CSV，以及 PNG / JPG / JPEG / BMP / GIF 五类图片。图片默认只登记元数据；扫描件 OCR 为可选能力，需安装 `requirements-optional.txt` 并显式开启 |
| **多路混合检索** | 向量检索（ChromaDB）+ 关键词检索（BM25），RRF 融合 |
| **智能重排序** | L2 精排优先调用托管重排（阿里云百炼 qwen3.7-text-rerank）；未配置 key 或调用失败时降级到本地 Cross-Encoder（BAAI/bge-reranker-base），再降级到 l1 规则重排。仅「综合分析 / 对比分析」两类查询走 LLM 重排（l3） |
| **流式对话** | SSE 流式输出，打字机效果，用户体验流畅 |
| **多轮会话记忆** | 会话持久化（Redis 可选，默认 JSON 落盘），支持上下文连续对话 |
| **多模型支持** | DeepSeek / 阿里云百炼，按配置切换 |
| **置信度评估** | 自动评估回答可信度，透明展示知识来源 |
| **引用追溯** | 每条回答精确标注引用片段，支持原文查看 |
| **Agent 深度模式** | `mode=agent`：多轮工具循环、空/错自动重试、终轮覆盖自检；支持 baseline / improved 双变体与五维能力评测 |
| **Docker Compose 开发部署** | 提供前端、后端和 Redis 的容器化启动配置；该链路未在本机验证，使用前请先跑通一次上传 |

---

## 技术架构

```mermaid
graph TB
    subgraph 前端
        A[React 18 + TypeScript]
        B[Ant Design 5]
        C[Vite 5]
    end

    subgraph 后端
        D[FastAPI + Uvicorn]
        E[ChromaDB 向量库]
        F[Redis 会话缓存]
        G[BM25 关键词检索]
    end

    subgraph AI 模型
        I[DeepSeek API]
        J[阿里云百炼]
    end

    subgraph 文档处理
        L[文档解析器]
        M[智能分块 Pipeline]
        N[嵌入模型]
    end

    A --> C
    A --> D
    D --> E
    D --> F
    D --> G
    D --> I
    D --> J
    L --> M
    M --> N
    N --> E

    style A fill:#1677ff,color:#fff
    style D fill:#52c41a,color:#fff
    style I fill:#722ed1,color:#fff
```

### 检索流程

```mermaid
sequenceDiagram
    participant U as 用户
    participant F as 前端
    participant B as FastAPI
    participant R as RAG 引擎
    participant V as 向量库
    participant L as LLM

    U->>F: 输入问题
    F->>B: POST /chat/stream
    B->>R: 意图识别 + 查询改写
    R->>V: 多路检索 (向量+BM25)
    V-->>R: 候选文档
    R->>R: Rerank 重排序
    R->>L: 组装 Prompt + 上下文
    L-->>R: 流式生成
    R-->>B: 逐 token 返回
    B-->>F: SSE 事件流
    F-->>U: 打字机效果展示
```

---

## 技术栈

| 层级 | 技术 | 选型理由 |
|------|------|----------|
| **前端框架** | React 18 + TypeScript | 类型安全、生态丰富、适合快速构建应用 |
| **UI 组件库** | Ant Design 5 | 开箱即用、适合快速搭建界面 |
| **构建工具** | Vite 5 | 极快的 HMR 和构建速度 |
| **后端框架** | FastAPI | 高性能异步、自动 API 文档、Pydantic 校验 |
| **向量数据库** | ChromaDB | 轻量嵌入、零配置、适合中小规模 |
| **缓存** | Redis 7 | 会话持久化 |
| **LLM 提供商** | DeepSeek API | 高性价比、中文能力强 |
| **重排模型** | qwen3.7-text-rerank / BAAI-bge-reranker-base | 托管优先、本地兜底，按可用性自动降级 |
| **嵌入模型** | BGE-small-zh-v1.5 | 中文语义匹配 SOTA |
| **容器化** | Docker + docker-compose | 方便本地开发、环境复现和功能验证 |

---

## 快速开始

### 前置要求

- Python 3.10+
- Node.js 18+
- Redis 7+（可选，用于会话持久化）
- DeepSeek API Key（或阿里云百炼 API Key）
- 阿里云百炼 API Key（可选，仅用于托管重排；不配则走本地 Cross-Encoder）

### 1. 克隆项目

```bash
git clone https://github.com/tuanzi188/KnowledgeMind.git
cd KnowledgeMind
```

### 2. 配置环境变量

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入 DS_API_KEY
```

### 3. Docker Compose 开发部署（可选）

提供前端、后端和 Redis 的容器化启动配置，方便本地开发、环境复现和功能验证。

```bash
docker compose up -d --build
# 网页：http://localhost:5173
# API 文档：http://localhost:8002/docs
```

### 4. 本地开发模式

**后端：**

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

**前端：**

```bash
cd frontend
npm ci
npm run dev
# 访问 http://localhost:5173
```

---

## 项目结构

```
KnowledgeMind/
├── frontend/                # React 前端
│   ├── src/
│   │   ├── api/             # API 客户端 & 类型定义
│   │   ├── components/      # 组件
│   │   │   ├── ChatBox.tsx          # 聊天主组件
│   │   │   ├── MessageItem.tsx      # 消息气泡
│   │   │   ├── ChatSidebar.tsx      # 侧边栏
│   │   │   ├── ChatHeader.tsx       # 顶部栏
│   │   │   ├── ChatInput.tsx        # 输入框
│   │   │   ├── CitationPanel.tsx    # 引用面板
│   │   │   ├── DocumentUpload.tsx   # 文档上传
│   │   │   └── DocumentManager.tsx  # 文档管理
│   │   ├── hooks/           # 自定义 Hooks
│   │   │   ├── useChat.ts           # 聊天逻辑
│   │   │   └── useConversations.ts  # 会话管理
│   │   ├── styles/          # 全局样式
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── package.json
│   └── vite.config.ts
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # API 路由
│   │   ├── agent/           # Agent runner（baseline / improved）与提示词
│   │   ├── tools/           # Agent 只读工具层（检索 / 回查 / 故障注入）
│   │   ├── core/            # 认证、安全
│   │   ├── models/          # 数据模型
│   │   ├── services/        # 核心服务
│   │   │   ├── rag_engine.py        # RAG 引擎
│   │   │   ├── hybrid_search.py     # 混合检索
│   │   │   ├── vector_store.py      # 向量存储
│   │   │   ├── reranker.py          # 重排序
│   │   │   ├── model_provider.py    # 模型提供商
│   │   │   └── ...
│   │   └── config.py        # 配置管理
│   ├── evaluation/          # Agent 评测：任务集、判分、pass^k、双变体对比
│   ├── requirements.txt
│   └── Dockerfile
├── docs/                    # 评测设计与证据（AGENT_EVAL_PROJECT_SPEC.md）
├── docker-compose.yml
├── README.md
└── .gitignore
```

---

## API 文档

启动后端后访问 http://localhost:8002/docs 查看 Swagger 自动生成的 API 文档。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/chat` | POST | 同步对话；`mode=agent` 走 Agent 深度模式 |
| `/api/v1/chat/stream` | POST | 流式对话 (SSE)；Agent 模式暂不支持 |
| `/api/v1/search` | POST | 语义搜索 |
| `/api/v1/upload` | POST | 上传文档 |
| `/api/v1/documents` | GET | 文档列表 |
| `/api/v1/documents/{id}` | DELETE | 删除文档 |
| `/api/v1/conversations` | GET | 会话列表 |
| `/api/v1/conversations/{id}` | GET | 会话详情 |
| `/api/v1/conversations/{id}` | DELETE | 删除会话 |
| `/api/v1/health` | GET | 健康检查 |
| `/api/v1/stats` | GET | 统计信息 |

---

## MCP 接口

检索能力同时以 MCP（Model Context Protocol）server 的形式对外提供，任何 MCP 兼容客户端（Cursor / Claude Desktop / LangGraph 等）都能接入：

```bash
cd backend
python -m app.mcp.server                                   # stdio（本机客户端）
python -m app.mcp.server --transport http --port 8931      # streamable-http
python -m app.mcp.demo_client                              # 客户端示例：握手 → 工具清单 → 检索 → 回查
```

工具：`search_kb(query, top_k)`（混合召回 + CE 重排）、`get_citation(chunk_id)`（按段落回查原文）。
默认读取真实向量库；设置 `MCP_DEMO=1` 时改用评测语料，便于零依赖演示。

> MCP 协议本身没有内建鉴权：stdio 模式靠进程隔离，http 模式默认只绑定 `127.0.0.1`；如需跨机使用，必须放在带 token 校验的网关之后。

## Agent 模式与能力评测

`POST /api/v1/chat` 支持 `mode=agent`（前端「深度检索」开关）。Agent 有两套实现，共用同一套证据纪律（只用检索结果、必须引用、不许编数字）：

| 变体 | 机制 | 用途 |
|---|---|---|
| `baseline` | 最多一轮工具调用；不重试、不纠错、不自检 | 对照组，测量“单轮决策上限” |
| `improved` | 多轮工具循环 + 空/错自动重试（query 改写）+ 终轮覆盖自检 | 线上默认；证明评测能驱动改进 |

评测口径：30 条任务 × 五维（检索与引用 / 多步拆解 / 拒答与边界 / 澄清与追问 / 恢复与纠错），每任务 3 次；确定性检查 + LLM judge；报告给 `pass@1`（任务均值）、`pass^3`（三次全过）与逐任务表；显著性只做整卷 30 条配对 McNemar，分维度只报 delta。

```bash
# 1) 跑原始结果（约 30–45 分钟含判分；--skip-index 复用已有演示语料）
cd backend
python -m evaluation.run_agent_eval --variant improved --runs 3 --skip-index \
  --out ../docs/evidence/agent_improved_runs3_20261027.json
# 2) 判分（含 LLM judge）
python -m evaluation.score_agent_runs --runs ../docs/evidence/agent_improved_runs3_20261027.json \
  --out ../docs/evidence/agent_improved_report3_20261027.json --concurrency 3
# 3) 双变体对比
python -m evaluation.compare_agent_reports \
  --baseline ../docs/evidence/agent_baseline_report3_20261027.json \
  --improved ../docs/evidence/agent_improved_report3_20261027.json
```

2026-10-27 实测（30 条 × 3 次 × 2 变体 = 180 次运行）：

| 指标 | baseline | improved |
|---|---|---|
| pass@1（任务均值） | 80.0% | 95.6% |
| pass^3 | 70.0% | 93.3% |
| 恢复与纠错 | 22.2% | 83.3%（delta 61.1 ≥ 该维 CI 宽度 53.3） |
| 拒答与边界 | 100% | 94.4%（N3 有 1 次把“没有扣款条款”答成确定结论） |
| token / 次运行 | 2,378 | 5,650（+137.6%） |
| 中位延迟 | 6.05 s | 10.81 s（+78.6%） |

整卷配对 McNemar：修好 5 / 弄坏 0，**p=0.0625（未达 0.05）**。准确表述应是“恢复维度有可分辨改进，整卷显著性因只有 5 个不一致对而不成立”，不能写成“全卷显著提升”。完整数据见 `docs/evidence/agent_compare3_20261027.json` 与 `docs/AGENT_EVAL_PROJECT_SPEC.md` §10.4。

## License

MIT

## 身份与权限

- 单用户开发模式：未配置认证时使用固定的 `anonymous` 身份，不读取客户端自报的用户、部门或角色。仅适合本机开发。
- 生产/预发模式：设置 `APP_ENV=production`（或 `staging`）后，若既未配置 `API_KEY` 也未配置 `AUTH_USERS_JSON`，服务会拒绝启动；`/api/v1/health/deep` 也要求认证。
- 单用户访问控制：后端设置 `API_KEY`，在网页右上角账号面板输入该令牌。不要使用 `VITE_API_KEY` 将共享密钥编译进前端。
- 多用户模式：在 `backend/.env` 配置 `AUTH_USERS_JSON`，为每人提供独立随机令牌、用户 ID、角色与部门。配置示例见 `backend/.env.example`。启用后共享 `API_KEY` 不再有效。
- 用户 ID 应保持稳定；同一用户 ID 可配置多个令牌用于轮换。角色仅由服务端授予，只有管理员可查看全局分析与审计。
- 访问令牌只保存在当前标签页的会话存储中；退出后清除。部署到公网时必须使用 HTTPS，并设置身份凭据。
- 每个用户的会话使用独立存储命名空间，列表、详情、续聊、流式对话和删除均隔离。历史无身份会话仅在原单用户模式下可见，不自动分配给新用户。
- 本轮是访问凭据与会话隔离加固，不包含注册、找回密码、企业单点登录或用户管理后台。

## 数据与升级

- 本地默认写入仓库根目录 `data/`；Docker 使用 `DATA_DIR=/app/data`，与宿主机 `./data` 挂载一致，会话文件也使用这个目录。
- 旧版本曾将部分数据写到仓库父目录。升级前停止服务，备份旧目录，并手动迁移到新的 `data/`；不要直接覆盖已有数据。也可临时将 `DATA_DIR` 指向旧目录。
- Redis 仅在容器网络内使用，不再向宿主机公开 6379 端口。
- `frontend/index.html` 加载 React 入口，构建产物在 `frontend/dist/`。
- 无外部模型凭据时可以运行单元测试；实际文档问答仍需可用的模型 API。

## 开发验证

```sh
cd backend
pip install -r requirements.txt
pip install pytest pytest-asyncio
python -m pytest -q
cd ../frontend
npm ci
npm test
npm run build
```

CI 同时运行后端测试、前端测试与生产构建。权限回归覆盖伪造角色、会话维度的跨用户读取与删除、流式会话隔离和非法会话编号；文档级 ACL 另有独立端到端测试（上传时共享给他人 → 被授权用户可见、未授权用户不可见）。
