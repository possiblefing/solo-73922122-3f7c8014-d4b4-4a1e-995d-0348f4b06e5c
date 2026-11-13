# 空气质量月度读数采集平台（FastAPI + Playwright）

从**只有网页导出功能的旧监测门户**取得站点月度 PM2.5 读数，供数据入湖使用。
采集服务通过**真实浏览器**（Playwright/Chromium）像人工一样选站、查询、
点击导出，**不直接调用门户的任何 HTTP 接口**。

仓库自带一个可延迟弹窗与下载的本地旧门户（含虚构读数），整套环境可用
Docker Compose 一键启动，并在 `/docs` 演示并行采集、取消与下载。

## 目录结构

```
.
├── portal/                 本地旧监测门户（仅 Python 标准库）
│   ├── app.py              表单/查询/新窗口导出 CSV，支持弹窗与下载延迟
│   └── Dockerfile
├── collector/              FastAPI + Playwright 采集服务
│   ├── main.py             受理/状态/取消/下载 HTTP 接口
│   ├── manager.py          任务状态机、60s 预算、取消/超时、暂存与清理
│   ├── browser.py          真实浏览器操作（每任务独立浏览器/上下文/下载）
│   ├── validation.py       CSV 完整性与逐行站号、UTC 月份校验
│   ├── models.py           请求/响应模型
│   ├── config.py           固定门户地址等配置（环境变量）
│   ├── requirements.txt
│   └── Dockerfile
├── tests/                  单测、ASGI 级测试、真实浏览器测试、e2e 脚本
├── docker-compose.yml      标准环境
├── docker-compose.slow.yml 慢门户覆盖层（演示取消与 60s 超时）
└── docs/DEMO.md            分步演示手册
```

## 快速开始

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 采集 API | http://localhost:8000 | OpenAPI 文档：http://localhost:8000/docs |
| 旧门户 | http://localhost:8080 | 可直接在浏览器查看老式页面 |

健康检查：`GET http://localhost:8000/healthz`。

### 演示取消与 60 秒超时

慢门户覆盖层把弹窗延迟设为 10s、下载延迟设为 90s：

```bash
docker compose -f docker-compose.yml -f docker-compose.slow.yml up --build
```

- 受理后立即调用取消接口 → 任务在浏览器与下载回收后进入 `cancelled`；
- 不取消、一直等待 → 任务在受理起 60 秒时进入 `timeout`。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/collect/jobs` | 受理采集任务，请求体 `{"station": "ST001", "month": "2026-10"}`，返回 202 与 `job_id` |
| GET | `/collect/jobs` | 列出全部任务 |
| GET | `/collect/jobs/{job_id}` | 查询状态（含 `deadline_at`、行数、字节数、错误信息、下载链接） |
| POST | `/collect/jobs/{job_id}/cancel` | 取消未结束任务；已结束返回 409 |
| GET | `/collect/jobs/{job_id}/download` | 仅成功且通过校验的任务可下载，否则 409/404/410 |

任务状态：`pending → running → succeeded | failed | cancelling → cancelled | timeout`。
其中 `cancelling` 表示取消已受理、浏览器工作正在回收；只有清理完成后才会
报告终态 `cancelled`。

## 关键设计与保证

1. **真实浏览器、不直连接口**：打开门户首页 → 在 `<select>` 选站 →
   填写 UTC 月份 → 提交查询表单 → 等待结果表 → 点击“导出 CSV”按钮 →
   等待新窗口产生的下载。全程页面操作，无任何门户 API 直调。
2. **任务间完全隔离**：每个任务启动独立 Chromium 进程与 BrowserContext
   （独立 Cookie/缓存/存储）、独立事件监听与独立暂存目录，两个任务可并行，
   会话、窗口、下载互不干扰。
3. **完整下载 + 逐行校验后才发布**：下载先写入私有暂存目录，校验通过后
   原子移动到任务发布目录。校验内容：UTF-8 可读、表头正确、每行站号等于
   请求站号、每行 UTC 采样时间落在请求月份、PM2.5 为非负数值。
4. **失败不留可下载残件**：校验失败、门户报错、取消、超时都会删除暂存目录，
   下载接口对非成功任务一律 409。
5. **60 秒预算从受理起算**：`JOB_TIMEOUT_SECONDS`（默认 60）到期任务进入
   `timeout`，迟到下载同样不得发布。
6. **取消语义**：取消一经受理（状态 `cancelling`）立即设置取消事件并主动
   关闭浏览器；此后即使下载迟到也不会发布。终态 `cancelled` 仅在浏览器
   工作、下载与暂存目录全部回收后才报告，不影响其他并行任务。
7. **门户地址固定配置**：通过环境变量 `PORTAL_URL` 配置，请求体只接受
   站号与月份。

## 本地（非 Docker）运行与测试

本地门户只依赖标准库：

```bash
PORTAL_PORT=8080 python -I portal/app.py
```

采集服务需要 Python 3.11+ 并安装 `collector/requirements.txt`，且
`python -m playwright install chromium` 已完成：

```bash
pip install -r collector/requirements.txt
python -m playwright install chromium
PORTAL_URL=http://127.0.0.1:8080 \
  uvicorn collector.main:app --host 0.0.0.0 --port 8000
```

测试：

```bash
python -I tests/test_validation.py           # CSV 校验单测（标准库）
python tests/test_manager.py                 # ASGI 级状态机/取消/超时（需 httpx）
python tests/test_real_browser.py            # 真实 Chromium 采集与取消
python -I tests/e2e_demo.py http://127.0.0.1:8000   # 全链路 e2e
SLOW=1 python -I tests/e2e_demo.py           # 含取消/超时（配慢门户）
```

## 门户演示数据

内置站点 `ST001`–`ST005` 输出 24 条/月的确定性虚构小时读数；`STBAD`
故意在 CSV 中写入错误站号，用于演示逐行校验拦截。未知站号（如 `STNOPE`）
会在门户下拉选项检查阶段被快速拒绝。
