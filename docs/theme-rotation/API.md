# 题材轮动 API 说明

版本：1.0 · 2026-09-24 · 模块：`theme-rotation`  
前缀约定：业务接口多在 `/v3/market/...`；开放复盘在 `/v3/open/...`。外层一般为 `{"code":200,"data":...}`；热点自研路由可带 `meta`。

## 0. 读写总则

| 规则 | 说明 |
|---|---|
| GET 只读 | 不在请求里触发历史回填；缺日返回空/missing |
| 日期 | 交易日；支持 `YYYY-MM-DD` 或 `YYYYMMDD` |
| plate_type | `17` 一级题材，`18` 二级题材；`14/15` 为东财独立分类，不映射 |
| 今日例外 | 横排未定稿可顺带 `refresh_intraday`；个股可 `is_real`；K 线校准开启时可按需缓存上游 |

---

## 1. 横排榜与趋势（主链 UI）

### `GET /v3/market/plates/{plate_type}/rank/columns`

题材轮动横排多列。

| 参数 | 说明 |
|---|---|
| `plate_type` | 17 / 15 / 14 |
| `date2` | 结束日，默认今天 |
| `days` | 列数，默认 10，上限 120 |
| `n_days` | 聚合窗口 1/3/5 |
| `n_type` | `9` 强度 / `1` 涨幅 / `3` 资金 |
| `limit` | TopN，默认 12 |

`data`：按日 `columns[]`，每列含 `date/status/rows`。历史只读 `plate_rank_daily`；今日未定稿时题材 17 可刷盘中预览。

### `GET /v3/market/plates/{plate_type}/rank/trend`

底部强度 / 成交额日序列。

| 参数 | 说明 |
|---|---|
| `plate_type` | 当前仅 **17** |
| `plate_code` | 一级题材码 |
| `day_start` / `day_end` | 区间 |

只读本地，不回源。

### `GET /v3/market/plates/{plate_type}/rank/days`

多日聚合扁平榜（兼容旧入口）。参数同 columns 语义侧的 `n_days/n_type/limit/date2`。

---

## 2. 二级与成分

### `GET /v3/market/plates/17/{plate_code}/sub-plates-stocks`

| 参数 | 说明 |
|---|---|
| `dates` | 逗号分隔交易日 |

响应：

```json
{
  "sub_plates": [{"code":"801xxx","name":"..."}],
  "stocks": {"2026-09-22": {"801xxx": ["600000", "..."], ...}},
  "stats": {"2026-09-22": {"801xxx": {"quote_rate":..., "limit_up_count":..., "limit_down_count":...}}}
}
```

---

## 3. 个股

### `GET /v3/market/plates/18/{plate_code}/stocks/rates`

二级个股涨幅榜。

| 参数 | 说明 |
|---|---|
| `date1` | 交易日 |
| `page` / `limit` | 分页 |
| `is_real` | `1` 用实时价刷新排序 |

### `GET /v3/market/plates/17/{plate_code}/stocks/rates`

一级全成分；今日无收盘日线时即使未传 `is_real` 也走实时。

---

## 4. 人气

### `GET /v3/market/plates/{plate_type}/{plate_code}/stocks/rank/list`

| 参数 | 说明 |
|---|---|
| `date1` | 交易日 |
| `page` / `limit` | 分页；题材内截断常见上限 300 |
| `with_pct` | 是否附带涨幅 |

语义：当日人气快照 ∩ 成分；`rank` 升序；缺快照不借邻日。响应应可区分 `status/meta`（如 `missing_popular_snapshot`）。

---

## 5. 涨幅分档

### `GET /v3/market/plates/17/{plate_code}/stocks/pct`

| 参数 | 说明 |
|---|---|
| `date1` | 期末日 |
| `days` | 窗口，默认 10 |

返回 ≥20% 五档桶；今日缺日线可用实时期末价。

### `GET /v3/market/plates/17/{plate_code}/stocks/pct/batch`

多日批量（产品侧可有登录/试用门槛）。`dates` 逗号分隔，单次建议 ≤15 日。

---

## 6. 梯队

### `GET /v3/open/review/uplimit/hot`

| 参数 | 说明 |
|---|---|
| `date1` | 交易日 |
| `board` | 一级题材码 `801xxx`；不传则为全市场矩阵形态 |
| `limit` | 条数上限 |

板内语义：涨停池 ∩ 题材成分 + 收盘清洗。前端二级过滤用当日二级成分截断。

自研路径另有：`GET /v3/market/hotspots/steps?board=&date1=&plate_type=17&sub_plate_code=`（`formula_id=ml_r1` 时走目录门禁）。

---

## 7. K 线

### `GET /v3/market/kline/plate/{code}`

### `GET /v3/market/kline/sub-plate/{code}`

| 参数 | 说明 |
|---|---|
| `n` | 根数，默认 250 |
| `date1` | 可选截止 |

契约字段（统一）：

| 字段 | 含义 |
|---|---|
| `x` | 日期序列 `YYYYMMDD` |
| `y` | OHLC 为 `[o,c,h,l,preclose]`；`daily_nav` 为标量净值 |
| `series_kind` | `ohlc` \| `daily_nav` |
| `status` / `source` / `as_of_date` | 可用性与来源 |
| `amount` / `turnover` | 板块侧 turnover 语义为成交额（元），勿当换手率 |

优先级：同花顺板指日线 → 代理板指 →（可选）校准 OHLC 缓存 → **成分等权日净值**。

热点自研：`GET /v3/market/hotspots/kline/{plate_code}?plate_type=&n=`。

---

## 8. 驱动消息

### `GET /v3/market/plate/popular/reason?plate_code=&limit=`

列表：标题、摘要、日期、msgid 等。本地 `plate_reason_daily` 优先。

### `GET /v3/market/plate/popular/reason/content?msgid=`

正文：`{ID, Title, Content, CreateTime}`。无结构化正文时回退 `boom_reason` / `title`。

---

## 9. 能力与自研排名（可选）

### `GET /v3/market/hotspots/capabilities`

区分「代码已实现」与「所选日期数据就绪」。

### `GET /v3/market/hotspots/plates/{plate_type}/rank/days|batch`

`formula_id=ml_r1` 时走自研强度；分类版本不匹配返回 **409**。无验证 17/18 目录时不得伪装成功。

---

## 10. 错误与状态约定

| HTTP / 业务 | 含义 |
|---|---|
| 422 | 参数/分类非法 |
| 401 / 403 | 需登录或 VIP（涨幅 batch 等） |
| 409 | 公式或 taxonomy 版本冲突 |
| `data.status=missing*` | 业务缺输入；仍可能 HTTP 200 |
| `unsupported_taxonomy` | 分类未就绪，不是空榜成功 |

客户端必须以列级 / 行级 `status` 与 `reasons` 为准，禁止仅凭 `code==200` 标绿。
