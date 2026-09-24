# market-api · 题材轮动行情服务

本目录是热点「题材轮动」的只读行情 API（原聊斋 `backend/`），由 `scripts/start` **统一拉起**，监听本机 `127.0.0.1:8766`。

浏览器只访问工作台 `http://127.0.0.1:5930`；Vite 把同源 `/v3/*` 代理到本服务，**不要**再单独开 8000 端口或手写 `http://127.0.0.1:8000`。

| 项 | 说明 |
|---|---|
| 入口 | `uvicorn app.main:app --host 127.0.0.1 --port 8766` |
| 库 | `zzquant.db`（本地事实；已 gitignore） |
| 解释器 | `.venv`（可与归档 venv 联接；或按 `requirements.txt` 重建） |
| 文档 | [`docs/theme-rotation/`](../docs/theme-rotation/README.md) |

环境变量（start 已设）：

- `ZZQUANT_ENABLE_ORIGIN_REFERENCE=1`：本地驱动消息为空时允许校准回源并落库
- `ZZQUANT_COLLECTOR_MODE=external`：API 进程不内嵌采集器
