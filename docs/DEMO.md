# 演示手册（/docs 与 curl）

先启动环境：

```bash
docker compose up --build
```

打开交互式文档：<http://localhost:8000/docs>。下面的命令均可直接复制执行，
也可以在 Swagger UI 中找到对应接口点击 **Try it out**。

## 1. 并行受理两个采集任务

在两个终端（或连续两次调用）分别提交不同站号。每个任务使用独立的
Chromium 进程、浏览器上下文、窗口与下载目录。

```bash
curl -s -X POST http://localhost:8000/collect/jobs \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST001","month":"2026-10"}'

curl -s -X POST http://localhost:8000/collect/jobs \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST002","month":"2026-10"}'
```

响应（202 Accepted）示例：

```json
{
  "job_id": "68b492144572407aa72f256e0c70fcc",
  "status": "pending",
  "message": "job accepted; 60s budget from acceptance; ..."
}
```

## 2. 轮询状态

```bash
curl -s http://localhost:8000/collect/jobs/<JOB_ID> | python -m json.tool
```

成功后响应包含行数、字节数与下载链接：

```json
{
  "job_id": "...",
  "station": "ST001",
  "month": "2026-10",
  "status": "succeeded",
  "rows": 24,
  "bytes": 820,
  "deadline_at": "2026-11-13T07:25:29.000+00:00",
  "download_url": "/collect/jobs/<JOB_ID>/download"
}
```

列出全部任务：

```bash
curl -s http://localhost:8000/collect/jobs | python -m json.tool
```

## 3. 下载已校验的 CSV

```bash
curl -OJ http://localhost:8000/collect/jobs/<JOB_ID>/download
head -3 aq_ST001_202610.csv
```

```
station,utc_time,pm25_ugm3
ST001,2026-10-01T00:00:00Z,13.6
ST001,2026-10-01T01:00:00Z,10.0
```

非成功任务调用下载接口返回 `409`，文件系统中也不存在任何残件。

## 4. 校验失败演示（坏数据站点）

`STBAD` 的门户 CSV 故意把站号写成 `ST999`，逐行校验会拦截：

```bash
curl -s -X POST http://localhost:8000/collect/jobs \
  -H 'Content-Type: application/json' \
  -d '{"station":"STBAD","month":"2026-10"}'
# 轮询至终态
curl -s http://localhost:8000/collect/jobs/<JOB_ID> | python -m json.tool
```

终态：

```json
{
  "status": "failed",
  "error": "csv validation failed: line 2: station mismatch: file has 'ST999', requested 'STBAD'"
}
```

未知站号则在选站前快速失败（约 1 秒）：

```json
{"status":"failed","error":"station 'STNOPE' is not offered by the portal (available: ST001, ...)"}
```

## 5. 取消演示

使用慢门户覆盖层启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.slow.yml up --build
```

受理一个任务，1～2 秒内取消：

```bash
JID=$(curl -s -X POST http://localhost:8000/collect/jobs \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST003","month":"2026-10"}' | python -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

sleep 1
curl -s -X POST http://localhost:8000/collect/jobs/$JID/cancel | python -m json.tool
```

取消受理后立即返回 `cancelling`，浏览器窗口被主动关闭：

```json
{"job_id":"...","status":"cancelling","cancelled":true,"message":"cancellation accepted"}
```

再次轮询，待浏览器工作与暂存清理完成后才出现终态：

```json
{"status":"cancelled","finished_at":"...","error":"cancelled by client; browser work and downloads reclaimed"}
```

- 终态后再取消 → `409`；
- 此时调用下载 → `409`；即使门户的迟到文件在几十秒后生成，也不会发布；
- 期间提交的其他任务不受影响，照常成功。

## 6. 60 秒超时演示

保持慢门户环境（下载延迟 90 秒），受理后**不要取消**，持续轮询：

```bash
curl -s -X POST http://localhost:8000/collect/jobs \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST004","month":"2026-10"}'
```

约 60 秒后任务进入 `timeout`，浏览器进程与暂存目录被回收，下载返回 `409`：

```json
{"status":"timeout","error":"job exceeded 60s acceptance budget"}
```

## 7. 输入校验

错误的月份格式在入口处被 Pydantic 拒绝（422），不会创建任务：

```bash
curl -i -X POST http://localhost:8000/collect/jobs \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST001","month":"10/2026"}'
```

## 自动化端到端脚本

```bash
# 标准门户：并行成功、坏数据、未知站、404/422
python -I tests/e2e_demo.py http://localhost:8000

# 慢门户：追加取消与超时用例
SLOW=1 python -I tests/e2e_demo.py http://localhost:8000
```
