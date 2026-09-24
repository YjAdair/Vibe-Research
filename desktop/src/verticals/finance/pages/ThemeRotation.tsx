import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { EChartsCoreOption } from "echarts/core";
import { cn } from "@/lib/utils";
import { EChart } from "@/components/ui/EChart";

const MODES = [
  { id: "stock", label: "个股" },
  { id: "ladder", label: "梯队" },
  { id: "popular", label: "人气" },
  { id: "pct", label: "涨幅" },
  { id: "kline", label: "K线" },
] as const;

type Mode = (typeof MODES)[number]["id"];
type SortKey = "score" | "rate" | "money";

type Cell = {
  plate_code: string;
  plate_name: string;
  sum_rate: number | null;
  sum_score: number | null;
  sum_leader_money: number | null;
};

type Column = { date: string; rows: Cell[]; status: string; collected_at?: string | null };

type TrendRow = {
  date1?: string;
  score?: number | null;
  trade_money?: number | null;
  money_leader?: number | null;
  money_leader_buy?: number | null;
  money_leader_sell?: number | null;
  plate_name?: string;
};

const NTYPE: Record<SortKey, number> = { score: 9, rate: 1, money: 3 };
const API = "/v3/market";
const OPEN = "/v3/open";
const RANK_URL = `${API}/plates/17/rank/columns`;
const TREND_URL = `${API}/plates/17/rank/trend`;
const POLL_MS = 60_000;

export function ThemeRotation() {
  // 默认对齐目标站 ML 题材轮动：10 日列、Top12、1 日周期、强度排序；资金关、强度/涨幅开；人气 30
  const [showMoney, setShowMoney] = useState(false);
  const [showScore, setShowScore] = useState(true);
  const [showRate, setShowRate] = useState(true);
  const [days, setDays] = useState(10);
  const [topN, setTopN] = useState(12);
  const [cycle, setCycle] = useState(1);
  const [sort, setSort] = useState<SortKey>("score");
  const [popularN, setPopularN] = useState(30);
  const [pctWindow, setPctWindow] = useState(10);
  const [pctScope, setPctScope] = useState<"cum" | "day">("cum");
  const [mode, setMode] = useState<Mode>("stock");
  const [picked, setPicked] = useState<string | null>(null);
  const [asOf, setAsOf] = useState("");
  const [board, setBoard] = useState<Column[]>([]);
  const [trend, setTrend] = useState<TrendRow[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [subCode, setSubCode] = useState<string | null>(null);
  const [subs, setSubs] = useState<{ code: string; name: string }[]>([]);

  const load = useCallback((showSpinner: boolean) => {
    const ctrl = new AbortController();
    const q = new URLSearchParams({
      days: String(days),
      n_days: String(cycle),
      n_type: String(NTYPE[sort]),
      limit: String(topN),
    });
    if (asOf) q.set("date2", asOf);
    if (showSpinner) setLoading(true);
    fetch(`${RANK_URL}?${q}`, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((body) => {
        setBoard(Array.isArray(body.data) ? body.data : []);
        setErr("");
      })
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError") setErr("题材榜没有读到");
      })
      .finally(() => { if (showSpinner) setLoading(false); });
    return () => ctrl.abort();
  }, [days, cycle, sort, topN, asOf]);

  useEffect(() => {
    return load(true);
  }, [load]);

  // 默认选最新日第 1 名；换榜后若原选已不在表里则回到第 1 名
  useEffect(() => {
    const first = board[0]?.rows[0]?.plate_code;
    if (!first) return;
    setPicked((prev) => {
      if (prev == null) return first;
      const still = board.some((c) => c.rows.some((r) => r.plate_code === prev));
      return still ? prev : first;
    });
  }, [board]);

  const needsPoll = board.some((c) => c.status === "partial_preview" || c.status === "stale");
  useEffect(() => {
    if (!needsPoll) return;
    const id = window.setInterval(() => load(false), POLL_MS);
    return () => window.clearInterval(id);
  }, [needsPoll, load]);

  // 与榜表同序：最新交易日在左
  const chartDates = useMemo(
    () => [...board.map((c) => c.date)].sort((a, b) => (a < b ? 1 : a > b ? -1 : 0)),
    [board],
  );

  useEffect(() => {
    if (!picked || chartDates.length === 0) {
      setTrend([]);
      return;
    }
    const ctrl = new AbortController();
    const dayEnd = chartDates[0] as string;
    const dayStart = chartDates[chartDates.length - 1] as string;
    const q = new URLSearchParams({
      plate_code: picked,
      day_start: dayStart,
      day_end: dayEnd,
    });
    fetch(`${TREND_URL}?${q}`, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((body) => setTrend(Array.isArray(body.data) ? body.data : []))
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError") setTrend([]);
      });
    return () => ctrl.abort();
  }, [picked, chartDates]);

  const pickedName = useMemo(() => {
    if (!picked) return "";
    for (const col of board) {
      const hit = col.rows.find((r) => r.plate_code === picked);
      if (hit) return hit.plate_name;
    }
    return trend[0]?.plate_name || picked;
  }, [picked, board, trend]);

  const strengthOption = useMemo(
    () => strengthChartOption(chartDates, trend),
    [chartDates, trend],
  );
  const amountOption = useMemo(
    () => amountChartOption(chartDates, trend),
    [chartDates, trend],
  );

  useEffect(() => {
    setSubCode(null);
  }, [picked]);

  useEffect(() => {
    if (!picked || chartDates.length === 0) {
      setSubs([]);
      return;
    }
    const ctrl = new AbortController();
    const dates = chartDates.slice(0, 8).join(",");
    fetch(`${API}/plates/17/${picked}/sub-plates-stocks?dates=${encodeURIComponent(dates)}`, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((body) => {
        const list = Array.isArray(body?.data?.sub_plates) ? body.data.sub_plates : [];
        const mapped = list
          .map((s: { code?: string; name?: string }) => ({
            code: String(s.code || ""),
            name: String(s.name || "").trim() || String(s.code || ""),
          }))
          .filter((s: { code: string }) => s.code && s.code !== picked);
        setSubs(mapped);
      })
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError") setSubs([]);
      });
    return () => ctrl.abort();
  }, [picked, chartDates]);

  const ranks = Array.from({ length: topN }, (_, i) => i + 1);

  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
        <Check on={showMoney} set={setShowMoney} label="资金" />
        <Check on={showScore} set={setShowScore} label="强度值" />
        <Check on={showRate} set={setShowRate} label="涨幅值" />
        <label className="inline-flex items-center gap-1 text-muted-foreground">
          截止
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
            className="rounded border border-border bg-transparent px-1.5 py-1" />
        </label>
        <Select label="天数" value={days} set={setDays} options={[6, 8, 10, 12, 15]} />
        <Select label="Top" value={topN} set={setTopN} options={[8, 10, 12, 20]} />
        <Select label="周期" value={cycle} set={setCycle} options={[1, 3, 5]} />
        <label className="inline-flex items-center gap-1 text-muted-foreground">
          排序
          <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)}
            className="rounded border border-border bg-transparent px-1.5 py-1">
            <option value="score">强度</option>
            <option value="rate">涨幅</option>
            <option value="money">资金</option>
          </select>
        </label>
        {mode === "popular" && <Select label="人气" value={popularN} set={setPopularN} options={[20, 30, 50, 100]} />}
        <button type="button" onClick={() => load(true)}
          className="rounded border border-border px-2 py-1 text-muted-foreground hover:text-foreground">
          刷新
        </button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        强度、涨幅、资金来自开盘啦精选板块榜，只读本地。{loading ? "读取中。" : ""}
        {needsPoll ? "今日列未定稿，约每分钟读一次本地。" : ""}
        {err}
        {cycle === 1 ? "" : " 周期只吃已定稿日；缺一天整列为空。"}
      </p>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="text-muted-foreground">
              <th className="sticky left-0 w-8 bg-background px-1 py-1 text-left">#</th>
              {board.map((col) => (
                <th key={col.date} className="min-w-[7.5rem] px-1 py-1 text-left font-normal">
                  {col.date.slice(5)}
                  <span className="ml-1 text-[10px] opacity-70">{statusLabel(col.status)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ranks.map((rank) => (
              <tr key={rank}>
                <td className="sticky left-0 bg-background px-1 py-1">
                  <span
                    className={cn(
                      "inline-flex h-5 w-5 items-center justify-center text-[11px]",
                      rank === 1 && "bg-[#e83b4b] text-white",
                      rank === 2 && "bg-[#f7bf16] text-[#202020]",
                      rank === 3 && "bg-[#4e8cff] text-white",
                      rank > 3 && "text-muted-foreground",
                    )}
                  >
                    {rank}
                  </span>
                </td>
                {board.map((col) => {
                  const cell = col.rows[rank - 1];
                  const on = !!cell && picked === cell.plate_code;
                  return (
                    <td key={col.date} className="p-0.5">
                      <button type="button" disabled={!cell} onClick={() => cell && setPicked(on ? null : cell.plate_code)}
                        className={cn("w-full rounded border px-1.5 py-1 text-left",
                          on ? "border-primary bg-primary/10" : "border-border/60 hover:bg-muted/40",
                          !cell && "text-muted-foreground")}>
                        <span className="block">{cell ? cell.plate_name : (col.status === "final" || col.status === "available" || col.status === "partial_preview" ? "—" : statusLabel(col.status))}</span>
                        {cell && (
                          <span className="block text-[10px] text-muted-foreground/80">
                            {showRate ? `(${fmtRate(cell.sum_rate)}) ` : ""}
                            {showScore ? `强度 ${fmtScore(cell.sum_score)} ` : ""}
                            {showMoney ? `资金 ${fmtMoney(cell.sum_leader_money)}` : ""}
                          </span>
                        )}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] text-muted-foreground">
        {picked == null ? "未选题材。点一个格子，同一代码会在各日期列一起高亮。" : `已选 ${picked}。`}
      </p>

      <div className="space-y-2">
        <div className="rounded border border-border/60 px-2 py-2">
          <div className="mb-1 text-xs font-medium">板块强度{pickedName ? ` · ${pickedName}` : ""}</div>
          {picked ? <EChart option={strengthOption} height={96} /> : (
            <p className="py-6 text-[11px] text-muted-foreground">点选题材后显示该板块的日级强度。</p>
          )}
        </div>
        <div data-testid="amount-trend" className="rounded border border-border/60 px-2 py-2">
          <div className="mb-1 text-xs font-medium">成交额{pickedName ? ` · ${pickedName}` : ""}</div>
          {picked ? <EChart option={amountOption} height={140} /> : (
            <p className="py-6 text-[11px] text-muted-foreground">柱＝成交额，折线＝净流入；买卖明细悬停看。资金开关不影响本图。</p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <button type="button" onClick={() => setSubCode(null)}
          className={cn("rounded px-2 py-0.5 text-xs", subCode == null ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-muted/40")}>
          全部
        </button>
        {subs.map((s) => (
          <button key={s.code} type="button" onClick={() => setSubCode(s.code)}
            className={cn("rounded px-2 py-0.5 text-xs", subCode === s.code ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-muted/40")}>
            {s.name}
          </button>
        ))}
        {picked && subs.length === 0 && (
          <span className="text-[11px] text-muted-foreground">二级：全部（无独立子码或尚未采到）</span>
        )}
      </div>

      <div className="flex gap-3">
        <div className="flex w-14 shrink-0 flex-col gap-1">
          {MODES.map((m) => (
            <button key={m.id} type="button" onClick={() => setMode(m.id)}
              className={cn("rounded px-1 py-1 text-xs", mode === m.id ? "bg-primary/15 font-medium text-primary" : "text-muted-foreground hover:bg-muted/50")}>
              {m.label}
            </button>
          ))}
        </div>
        <div className="min-w-0 flex-1 rounded border border-border/60 p-2 text-xs">
          {mode === "pct" && (
            <PctScopeBar
              scope={pctScope}
              setScope={setPctScope}
              windowDays={pctWindow}
              setWindowDays={setPctWindow}
            />
          )}
          <ModePanel
            mode={mode}
            plateCode={picked}
            subCode={subCode}
            dates={board.map((c) => c.date)}
            popularN={popularN}
            pctWindow={pctWindow}
            pctScope={pctScope}
          />
        </div>
      </div>
    </div>
  );
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function compactAmount(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  let display = value;
  let unit = "";
  if (absolute >= 1e8) {
    display = value / 1e8;
    unit = "亿";
  } else if (absolute >= 1e4) {
    display = value / 1e4;
    unit = "万";
  }
  const digits = Math.abs(display) >= 100 ? 0 : Math.abs(display) >= 10 ? 1 : 2;
  return `${display.toFixed(digits).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "")}${unit}`;
}

/** 板指/净值：控制小数位，避免浮窗和纵轴出现过长小数。 */
function fmtPrice(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const a = Math.abs(value);
  if (a >= 1000) return value.toFixed(1);
  if (a >= 100) return value.toFixed(2);
  return value.toFixed(2);
}

function compactVolume(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  if (absolute >= 1e8) return `${(value / 1e8).toFixed(2).replace(/\.?0+$/, "")}亿手`;
  if (absolute >= 1e4) return `${(value / 1e4).toFixed(1).replace(/\.0$/, "")}万手`;
  return `${Math.round(value)}手`;
}

function klineSourceLabel(source: unknown): string {
  const s = String(source || "");
  if (s === "plate_kline_origin") return "校准日线";
  if (s === "ew_member_daily_close") return "成分等权";
  if (s === "ths_board_daily") return "同花顺日线";
  if (s.includes("plate_proxy") || s.includes("published")) return "代理指数";
  return "";
}

function rowsByDate(dates: string[], trend: TrendRow[]) {
  const map = new Map(trend.map((r) => [String(r.date1), r]));
  return dates.map((d) => map.get(d) || null);
}

function strengthChartOption(dates: string[], trend: TrendRow[]): EChartsCoreOption {
  const mapped = rowsByDate(dates, trend);
  return {
    animation: false,
    grid: { left: 8, right: 8, top: 28, bottom: 8 },
    xAxis: { type: "category", data: dates, show: false, boundaryGap: true },
    yAxis: { type: "value", show: false, scale: true },
    tooltip: {
      trigger: "axis",
      formatter: (params: unknown) => {
        const item = Array.isArray(params) ? params[0] : params;
        const p = item as { axisValue?: string; value?: number | null };
        return `${p.axisValue || ""}<br/>强度：${p.value == null ? "—" : p.value}`;
      },
    },
    series: [{
      name: "板块强度",
      type: "line",
      smooth: true,
      data: mapped.map((row) => {
        const value = num(row?.score);
        return {
          value,
          label: { color: value != null && value < 0 ? "#43a66b" : "#d84a4a" },
        };
      }),
      symbol: "circle",
      symbolSize: 6,
      lineStyle: { color: "#999", width: 1 },
      itemStyle: { color: "#999" },
      label: {
        show: true,
        fontSize: 10,
        formatter: (p: { value?: number | null }) => (p.value == null ? "" : String(Math.round(p.value))),
      },
    }],
  };
}

function amountChartOption(dates: string[], trend: TrendRow[]): EChartsCoreOption {
  // 柱＝成交额；折线＝净流入。买卖只进 tooltip，避免柱顶多行标签互挡。
  const mapped = rowsByDate(dates, trend);
  const net = mapped.map((row) => num(row?.money_leader));
  return {
    animation: false,
    grid: { left: 8, right: 8, top: 28, bottom: 8 },
    xAxis: { type: "category", data: dates, show: false, boundaryGap: true },
    yAxis: [
      { type: "value", show: false, scale: true },
      { type: "value", show: false, scale: true },
    ],
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params : [params];
        const date = (list[0] as { axisValue?: string } | undefined)?.axisValue || "";
        const idx = dates.indexOf(String(date));
        const row = idx >= 0 ? mapped[idx] : null;
        if (!row) return `${date}<br/>暂无数据`;
        return `${date}<br/>成交额：${compactAmount(num(row.trade_money))}<br/>买入：${compactAmount(num(row.money_leader_buy))}<br/>卖出：${compactAmount(num(row.money_leader_sell))}<br/>净流入：${compactAmount(num(row.money_leader))}`;
      },
    },
    series: [
      {
        name: "成交额",
        type: "bar",
        data: mapped.map((row) => num(row?.trade_money)),
        itemStyle: { color: "#94a3b8" },
        barMaxWidth: 18,
        label: {
          show: true,
          position: "top",
          fontSize: 9,
          color: "#888",
          formatter: (p: { value?: number | null }) => compactAmount(p.value),
        },
      },
      {
        name: "净流入",
        type: "line",
        yAxisIndex: 1,
        data: net.map((v) => ({
          value: v,
          label: { color: v != null && v < 0 ? "#43a66b" : "#d84a4a" },
        })),
        symbol: "circle",
        symbolSize: 5,
        lineStyle: { color: "#c9a227", width: 1.5 },
        itemStyle: { color: "#c9a227" },
        label: {
          show: true,
          position: "bottom",
          distance: 4,
          fontSize: 9,
          formatter: (p: { value?: number | null }) => {
            if (p.value == null) return "";
            const sign = p.value > 0 ? "+" : "";
            return `净${sign}${compactAmount(p.value)}`;
          },
        },
        z: 3,
      },
    ],
  };
}

function statusLabel(status: string) {
  if (status === "final") return "定稿";
  if (status === "partial_preview") return "预览";
  if (status === "stale") return "过期";
  if (status === "missing_input") return "缺失";
  return "";
}

function fmtRate(v: number | null) {
  return typeof v === "number" ? v.toFixed(2) : "—";
}

function fmtScore(v: number | null) {
  return typeof v === "number" ? String(Math.round(v)) : "—";
}

function fmtMoney(v: number | null) {
  return typeof v === "number" ? `${(v / 1e8).toFixed(2)}亿` : "—";
}

/** 涨幅面板内口径条：一行紧凑分段，不占竖向空间 */
function PctScopeBar({
  scope, setScope, windowDays, setWindowDays,
}: {
  scope: "cum" | "day";
  setScope: (v: "cum" | "day") => void;
  windowDays: number;
  setWindowDays: (v: number) => void;
}) {
  const hint = scope === "day"
    ? "当日 ≥5% · 异/余=严重异动提醒"
    : `${windowDays}日累计 · 异/余=严重异动提醒`;
  return (
    <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/40 pb-1.5">
      <div
        role="group"
        aria-label="涨幅口径"
        className="inline-flex rounded-md border border-border/60 bg-muted/20 p-0.5"
      >
        {([
          { id: "day" as const, label: "当日" },
          { id: "cum" as const, label: "累计" },
        ]).map((opt) => (
          <button
            key={opt.id}
            type="button"
            onClick={() => setScope(opt.id)}
            className={cn(
              "rounded px-2 py-0.5 text-[11px] font-medium transition",
              scope === opt.id
                ? opt.id === "day"
                  ? "bg-red-500/15 text-red-600"
                  : "bg-amber-500/15 text-amber-700 dark:text-amber-500"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {opt.label}
          </button>
        ))}
      </div>
      {scope === "cum" && (
        <div
          role="group"
          aria-label="累计窗口"
          className="inline-flex items-center gap-0.5 rounded-md border border-amber-500/25 bg-amber-500/5 p-0.5"
        >
          {([5, 10, 20] as const).map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setWindowDays(n)}
              title={`${n} 个交易日`}
              className={cn(
                "min-w-6 rounded px-1.5 py-0.5 text-[11px] tabular-nums transition",
                windowDays === n
                  ? "bg-amber-500 text-white"
                  : "text-muted-foreground hover:text-amber-700 dark:hover:text-amber-400",
              )}
            >
              {n}
            </button>
          ))}
          <span className="pr-1 text-[10px] text-muted-foreground">日</span>
        </div>
      )}
      <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground" title={hint}>
        {hint}
      </span>
    </div>
  );
}

function ModePanel({
  mode, plateCode, subCode, dates, popularN, pctWindow, pctScope,
}: {
  mode: Mode;
  plateCode: string | null;
  subCode: string | null;
  dates: string[];
  popularN: number;
  pctWindow: number;
  pctScope: "cum" | "day";
}) {
  if (!plateCode || dates.length === 0) {
    return <p className="text-muted-foreground">先选题材。模式按榜表日期分列，与上方天数一致。</p>;
  }

  if (mode === "kline") {
    return <KlinePane plateCode={plateCode} subCode={subCode} asOf={dates[0]} />;
  }

  const latestDay = dates[0];

  return (
    <div className="flex gap-1.5 overflow-x-auto pb-1">
      {dates.map((day, idx) => (
        <div
          key={`${mode}-${plateCode}-${subCode ?? "all"}-${day}-${pctScope}`}
          className={cn(
            "shrink-0",
            mode === "pct" ? "w-[9.75rem]" : mode === "popular" ? "w-[11rem]" : "w-[9.75rem]",
          )}
        >
          {mode !== "pct" && (
            <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
              <span>{day.slice(5)}</span>
              {day === latestDay && (
                <span className="text-[10px] text-primary/80">
                  {mode === "stock" ? "实时" : "今日"}
                </span>
              )}
            </div>
          )}
          <LazyCol eager={idx < 2} placeholder={idx === 0 ? "加载今日…" : "滚动加载…"}>
            {mode === "stock" && (
              <StockDayList
                plateCode={plateCode}
                subCode={subCode}
                day={day}
                live={day === latestDay}
                pageSize={30}
              />
            )}
            {mode === "popular" && (
              <PagedDayList
                kind="popular"
                plateCode={plateCode}
                subCode={subCode}
                day={day}
                pageSize={30}
                maxRows={popularN}
              />
            )}
            {mode === "ladder" && (
              <LadderDay plateCode={plateCode} subCode={subCode} day={day} />
            )}
            {mode === "pct" && (
              <PctDay
                plateCode={plateCode}
                subCode={subCode}
                day={day}
                pctWindow={pctWindow}
                pctScope={pctScope}
                live={day === latestDay}
              />
            )}
          </LazyCol>
        </div>
      ))}
    </div>
  );
}

const PAGE_MISS = "缺成分/行情";

/** 历史日只读结果会话缓存（今日实时不入缓存）。后端仍是权威落库方。 */
const histMemo = new Map<string, { at: number; body: unknown }>();
const HIST_TTL_MS = 10 * 60_000;

async function fetchHistJson(url: string, signal?: AbortSignal): Promise<unknown> {
  const hit = histMemo.get(url);
  if (hit && Date.now() - hit.at < HIST_TTL_MS) return hit.body;
  const body = await fetch(url, { signal }).then((r) => r.json());
  histMemo.set(url, { at: Date.now(), body });
  if (histMemo.size > 200) {
    const first = histMemo.keys().next().value;
    if (first) histMemo.delete(first);
  }
  return body;
}

/** 列进入视口才挂载请求，避免 15 列 + 今日 is_real 同时打满后端。 */
function LazyCol({
  eager, placeholder, children,
}: {
  eager?: boolean;
  placeholder?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [on, setOn] = useState(!!eager);
  useEffect(() => {
    if (on) return;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (ents) => {
        if (!ents.some((e) => e.isIntersecting)) return;
        setOn(true);
        io.disconnect();
      },
      { root: null, rootMargin: "160px", threshold: 0.01 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [on]);
  if (on) return <>{children}</>;
  return (
    <div
      ref={ref}
      className="flex h-64 items-center justify-center rounded border border-border/40 text-[11px] text-muted-foreground"
    >
      {placeholder || "滚动加载…"}
    </div>
  );
}

function pctTone(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n) || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-red-600" : "text-emerald-600";
}

function fmtRankDiff(v: unknown): string {
  if (v == null || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "=";
  return n > 0 ? `+${n}` : String(n);
}

function fmtPct(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const s = n.toFixed(2);
  return n > 0 ? `+${s}%` : `${s}%`;
}

/** 个股列：历史本地 / 最新日实时；底部分页；行=序号+名称+带色涨幅 */
function StockDayList({
  plateCode, subCode, day, live, pageSize,
}: {
  plateCode: string;
  subCode: string | null;
  day: string;
  live: boolean;
  pageSize: number;
}) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const genRef = useRef(0);
  const inFlightRef = useRef(false);
  const code = subCode || plateCode;
  const plateType = subCode ? 18 : 17;

  const loadPage = useCallback(async (next: number, signal: AbortSignal, reset: boolean) => {
    if (inFlightRef.current && !reset) return;
    const gen = genRef.current;
    inFlightRef.current = true;
    setLoading(true);
    try {
      const q = new URLSearchParams({
        date1: day,
        page: String(next),
        limit: String(pageSize),
      });
      if (live) q.set("is_real", "1");
      const url = `${API}/plates/${plateType}/${code}/stocks/rates?${q}`;
      const body = (live
        ? await fetch(url, { signal }).then((r) => r.json())
        : await fetchHistJson(url, signal)) as { data?: { list?: Record<string, unknown>[]; total?: number } };
      if (signal.aborted || gen !== genRef.current) return;
      const list = Array.isArray(body?.data?.list) ? body.data!.list! : [];
      const tot = Number(body?.data?.total) || 0;
      setTotal(tot);
      setRows((prev) => (reset ? list : [...prev, ...list]));
      setPage(next);
      if (list.length || !reset) setErr("");
      else setErr(live ? "盘中实时拉取中/暂无（收盘后走日线）" : "缺收盘日线");
    } catch (e: unknown) {
      if ((e as { name?: string })?.name !== "AbortError" && gen === genRef.current) {
        setErr(live ? "实时行情超时，稍后重试" : "读取失败");
      }
    } finally {
      inFlightRef.current = false;
      if (gen === genRef.current) setLoading(false);
    }
  }, [pageSize, plateType, code, day, live]);

  useEffect(() => {
    const ctrl = new AbortController();
    genRef.current += 1;
    inFlightRef.current = false;
    setRows([]);
    setPage(0);
    setTotal(0);
    setErr("");
    // 今日实时很重：稍晚启动，让同屏历史列先吃本地库
    const delay = live ? 400 : 0;
    const t = window.setTimeout(() => {
      void loadPage(1, ctrl.signal, true);
    }, delay);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [loadPage, live]);

  useEffect(() => {
    const root = boxRef.current;
    const tip = sentinelRef.current;
    if (!root || !tip) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        if (inFlightRef.current || err) return;
        if (rows.length === 0 || rows.length >= total) return;
        void loadPage(page + 1, new AbortController().signal, false);
      },
      { root, rootMargin: "48px", threshold: 0 },
    );
    io.observe(tip);
    return () => io.disconnect();
  }, [err, rows.length, total, page, loadPage]);

  return (
    <div
      ref={boxRef}
      className="h-64 overflow-y-auto rounded border border-border/40 px-1 py-0.5"
    >
      {rows.map((row, i) => {
        const rate = row.px_change_rate;
        const rank = i + 1;
        return (
          <div
            key={`${String(row.stock_code)}-${i}`}
            className="flex items-center gap-1 border-b border-border/30 py-0.5 text-[11px] leading-tight"
          >
            <span
              className={cn(
                "inline-flex h-4 w-4 shrink-0 items-center justify-center text-[10px]",
                rank === 1 && "bg-[#e83b4b] text-white",
                rank === 2 && "bg-[#f7bf16] text-[#202020]",
                rank === 3 && "bg-[#4e8cff] text-white",
                rank > 3 && "text-muted-foreground",
              )}
            >
              {rank}
            </span>
            <span className="min-w-0 flex-1 truncate" title={String(row.stock_code || "")}>
              {String(row.stock_name || row.stock_code || "—")}
            </span>
            <span className={cn("w-[2.85rem] shrink-0 text-right tabular-nums", pctTone(rate))}>
              {fmtPct(rate)}
            </span>
          </div>
        );
      })}
      <div ref={sentinelRef} className="h-1" />
      {loading && <p className="py-1 text-[10px] text-muted-foreground">加载…</p>}
      {!loading && err && <p className="py-2 text-[10px] text-muted-foreground">{err}</p>}
      {!loading && !err && rows.length > 0 && rows.length >= total && (
        <p className="py-1 text-center text-[10px] text-muted-foreground">到底了 · {total}</p>
      )}
    </div>
  );
}

function PagedDayList({
  kind, plateCode, subCode, day, pageSize, maxRows,
}: {
  kind: "stock" | "popular";
  plateCode: string;
  subCode: string | null;
  day: string;
  pageSize: number;
  maxRows?: number;
}) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);
  const genRef = useRef(0);
  const code = subCode || plateCode;
  const plateType = subCode ? 18 : 17;
  const cap = maxRows ?? Number.POSITIVE_INFINITY;

  useEffect(() => {
    const ctrl = new AbortController();
    const gen = ++genRef.current;
    setRows([]);
    setPage(0);
    setTotal(0);
    setErr("");
    setLoading(true);
    const url = kind === "stock"
      ? `${API}/plates/${plateType}/${code}/stocks/rates?date1=${day}&page=1&limit=${pageSize}`
      : `${API}/plates/${plateType}/${code}/stocks/rank/list?date1=${day}&page=1&limit=${pageSize}&with_pct=1`;
    fetchHistJson(url, ctrl.signal)
      .then((bodyUnknown) => {
        const body = bodyUnknown as {
          data?: {
            list?: Record<string, unknown>[];
            total?: number;
            status?: string;
            meta?: { status?: string; note?: string };
          };
        };
        if (gen !== genRef.current) return;
        const list = Array.isArray(body?.data?.list) ? body.data!.list! : [];
        const tot = Math.min(Number(body?.data?.total) || 0, cap);
        const status = body?.data?.status || body?.data?.meta?.status;
        const note = typeof body?.data?.meta?.note === "string" ? body.data.meta.note : "";
        setTotal(tot);
        setRows(list.slice(0, cap));
        setPage(1);
        if (list.length) {
          setErr(kind === "popular" && note ? note : "");
        } else if (status === "missing_popular_snapshot") {
          setErr(note || "该日无人气快照（收盘采集后才有）");
        } else if (status === "missing_members") {
          setErr(note || "缺少该日题材成分");
        } else {
          setErr(PAGE_MISS);
        }
      })
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError" && gen === genRef.current) setErr("读取失败");
      })
      .finally(() => { if (gen === genRef.current) setLoading(false); });
    return () => ctrl.abort();
  }, [kind, plateType, code, day, pageSize, cap]);

  const loadMore = () => {
    if (loading || rows.length >= total || rows.length >= cap) return;
    const next = page + 1;
    const gen = genRef.current;
    setLoading(true);
    const url = kind === "stock"
      ? `${API}/plates/${plateType}/${code}/stocks/rates?date1=${day}&page=${next}&limit=${pageSize}`
      : `${API}/plates/${plateType}/${code}/stocks/rank/list?date1=${day}&page=${next}&limit=${pageSize}&with_pct=1`;
    fetch(url)
      .then((r) => r.json())
      .then((body) => {
        if (gen !== genRef.current) return;
        const list = Array.isArray(body?.data?.list) ? body.data.list as Record<string, unknown>[] : [];
        setRows((prev) => [...prev, ...list].slice(0, cap));
        setPage(next);
        if (body?.data?.total != null) setTotal(Math.min(Number(body.data.total) || 0, cap));
      })
      .catch(() => { /* keep */ })
      .finally(() => { if (gen === genRef.current) setLoading(false); });
  };

  const onScroll = () => {
    const el = boxRef.current;
    if (!el || loading) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) loadMore();
  };

  return (
    <div
      ref={boxRef}
      onScroll={onScroll}
      className="h-64 overflow-y-auto rounded border border-border/40 px-1 py-0.5"
    >
      {rows.map((row, i) => (
        <div key={`${String(row.stock_code)}-${i}`} className="grid grid-cols-[1.25rem_minmax(0,1fr)_1.5rem_2.75rem] items-center gap-x-0.5 border-b border-border/25 px-0.5 py-px text-[11px] leading-5 hover:bg-muted/30">
          <span className="text-right text-[10px] text-muted-foreground tabular-nums">
            {kind === "popular" && row.rank != null ? String(row.rank) : String(i + 1)}
          </span>
          <span
            className="truncate"
            title={`${String(row.stock_name || "")} ${String(row.stock_code || "")}`.trim()}
          >
            {String(row.stock_name || row.stock_code || "—")}
          </span>
          {kind === "popular" ? (
            <span className={cn("text-right text-[10px] tabular-nums font-medium", pctTone(row.rank_diff))}>
              {fmtRankDiff(row.rank_diff)}
            </span>
          ) : (
            <span />
          )}
          <span className={cn("text-right tabular-nums", pctTone(row.px_change_rate))}>
            {fmtPct(row.px_change_rate)}
          </span>
        </div>
      ))}
      {loading && <p className="py-1 text-[10px] text-muted-foreground">加载…</p>}
      {!loading && err && <p className="py-2 text-[10px] text-muted-foreground">{err}</p>}
      {!loading && !err && rows.length > 0 && rows.length >= Math.min(total, cap) && (
        <p className="py-1 text-center text-[10px] text-muted-foreground">到底了</p>
      )}
      {!loading && !err && rows.length > 0 && kind === "popular" && (
        <p className="py-1 text-[9px] text-muted-foreground/80">公开榜∩成分 · 名次变化0显示=</p>
      )}
    </div>
  );
}

function LadderDay({
  plateCode, subCode, day,
}: {
  plateCode: string;
  subCode: string | null;
  day: string;
}) {
  type Row = {
    stock_code?: string;
    stock_name?: string;
    up_limit_keep_times?: number | null;
    up_limit_time?: string;
    up_limit_type?: string;
    up_limit_desc?: string;
  };
  const [groups, setGroups] = useState<{ key: string; label: string; rows: Row[] }[]>([]);
  const [maxKeep, setMaxKeep] = useState<number | null>(null);
  const [note, setNote] = useState("读取…");

  useEffect(() => {
    const ctrl = new AbortController();
    setNote("读取…");
    setGroups([]);
    setMaxKeep(null);

    const hot = fetchHistJson(
      `${OPEN}/review/uplimit/hot?board=${encodeURIComponent(plateCode)}&date1=${day}`,
      ctrl.signal,
    );

    const members = subCode
      ? fetchHistJson(
          `${API}/plates/17/${encodeURIComponent(plateCode)}/sub-plates-stocks?dates=${day}`,
          ctrl.signal,
        )
      : Promise.resolve(null);

    Promise.all([hot, members])
      .then(([body, subBody]) => {
        const data = body?.data || {};
        const status = data.status || data.meta?.status;
        const raw = (data.plate_stocks?.[plateCode] || []) as Row[];
        let list = raw;
        if (subCode) {
          const codes = subBody?.data?.stocks?.[day]?.[subCode];
          if (!Array.isArray(codes)) {
            setGroups([]);
            setNote("该二级当日无成分");
            return;
          }
          const set = new Set(codes.map(String));
          list = raw.filter((s) => set.has(String(s.stock_code)));
        }
        if (status === "missing") {
          setGroups([]);
          setNote(PAGE_MISS);
          return;
        }
        const cleanNote = typeof data.meta?.note === "string" ? data.meta.note : "";
        const by: Record<string, Row[]> = {};
        let max: number | null = null;
        for (const s of list) {
          const n = typeof s.up_limit_keep_times === "number" ? s.up_limit_keep_times : null;
          const key = n === null ? "unknown" : String(n);
          (by[key] || (by[key] = [])).push(s);
          if (n !== null) max = max === null ? n : Math.max(max, n);
        }
        const ordered = Object.keys(by)
          .sort((a, b) => (b === "unknown" ? -1 : Number(b)) - (a === "unknown" ? -1 : Number(a)))
          .map((key) => ({
            key,
            label: key === "unknown" ? "连板缺失" : key === "0" ? "炸板" : `${key}板`,
            rows: by[key],
          }));
        setGroups(ordered);
        setMaxKeep(max);
        // 有池无交集 ≠ 缺成分；空列表单独提示
        setNote(ordered.length ? cleanNote : (status === "ok" ? "当日题材内无涨停/炸板" : PAGE_MISS));
      })
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError") setNote("读取失败");
      });
    return () => ctrl.abort();
  }, [plateCode, subCode, day]);

  return (
    <div className="h-56 overflow-y-auto rounded border border-border/40 px-1 py-0.5">
      {maxKeep != null && (
        <div className="mb-0.5 text-[10px] text-muted-foreground">最高{maxKeep}板</div>
      )}
      {note && groups.length > 0 && (
        <div className="mb-0.5 text-[10px] text-muted-foreground/80">{note}</div>
      )}
      {groups.map((g) => (
        <div key={g.key} className="mb-1">
          <div className="text-[10px] font-medium text-primary/80">{g.label}</div>
          {g.rows.map((row) => (
            <div
              key={String(row.stock_code)}
              className="flex items-baseline justify-between gap-1 border-b border-border/30 py-0.5 text-[11px]"
            >
              <span className="min-w-0 truncate">
                {row.up_limit_type ? (
                  <span className="mr-0.5 text-[10px] text-muted-foreground">[{row.up_limit_type}]</span>
                ) : null}
                {String(row.stock_name || row.stock_code || "—")}
                {row.up_limit_desc ? (
                  <span className="ml-0.5 text-[10px] text-muted-foreground">{row.up_limit_desc}</span>
                ) : null}
              </span>
              <span className="shrink-0 text-[10px] text-muted-foreground">
                {row.up_limit_time || "—"}
              </span>
            </div>
          ))}
        </div>
      ))}
      {!groups.length && <p className="py-2 text-[10px] text-muted-foreground">{note}</p>}
    </div>
  );
}

function dayPctBucket(pct: number): string | null {
  if (pct < 5) return null;
  if (pct < 7) return "5-7";
  if (pct < 10) return "7-10";
  if (pct < 15) return "10-15";
  if (pct < 20) return "15-20";
  return "20+";
}

function pctBucketLabel(key: string): string {
  if (key.endsWith("+")) return key;
  const lo = key.split("-")[0];
  return lo ? `${lo}+` : key;
}

const DAY_PCT_ORDER = ["20+", "15-20", "10-15", "7-10", "5-7"];

/** 涨幅列旁严重异动提醒（详情页再展开）。
 * 名单来自 movement 近阈值池（≤55%）；行标：异=监控 / 触=已达 / 余N=距阈值。
 */
type MoveHint = { label: string; tone: string; tip: string };

function moveHintOf(row: Record<string, unknown> | undefined): MoveHint | null {
  if (!row) return null;
  const t1 = Number(row.t1_space_pct);
  const t2 = Number(row.t2_space_pct);
  const spaces = [t1, t2].filter(Number.isFinite);
  const space = spaces.length ? Math.min(...spaces) : null;
  const tipParts = [
    Number.isFinite(t1) ? `10日偏离余${t1}%` : null,
    Number.isFinite(t2) ? `30日偏离余${t2}%` : null,
  ].filter(Boolean);
  if (row.is_monitored) {
    return {
      label: "异",
      tone: "text-red-600 dark:text-red-400",
      tip: ["严重异动监控中", ...tipParts].join(" · "),
    };
  }
  if (space == null) return null;
  if (space <= 0) {
    return {
      label: "触",
      tone: "text-red-600 dark:text-red-400",
      tip: ["已达严重异动阈值", ...tipParts].join(" · "),
    };
  }
  // 近阈值池内都提醒（原站黄标「即将」可到十余个百分点，不卡死在 10）
  return {
    label: `余${Math.round(space)}`,
    tone: space <= 10
      ? "text-amber-700 dark:text-amber-400"
      : "text-amber-600/80 dark:text-amber-500/80",
    tip: tipParts.join(" · ") || `距严重异动余${space}%`,
  };
}

function PctDay({
  plateCode, subCode, day, pctWindow, pctScope, live,
}: {
  plateCode: string;
  subCode: string | null;
  day: string;
  pctWindow: number;
  pctScope: "cum" | "day";
  live: boolean;
}) {
  const [buckets, setBuckets] = useState<Record<string, Record<string, unknown>[]>>({});
  const [moves, setMoves] = useState<Record<string, Record<string, unknown>>>({});
  const [order, setOrder] = useState<string[]>(
    pctScope === "day" ? DAY_PCT_ORDER : ["100+", "80-100", "60-80", "40-60", "20-40"],
  );
  const [note, setNote] = useState("读取…");

  useEffect(() => {
    const ctrl = new AbortController();
    setBuckets({});
    setMoves({});
    setNote("读取…");
    setOrder(pctScope === "day" ? DAY_PCT_ORDER : ["100+", "80-100", "60-80", "40-60", "20-40"]);

    const memReq = subCode
      ? fetchHistJson(
          `${API}/plates/17/${encodeURIComponent(plateCode)}/sub-plates-stocks?dates=${day}`,
          ctrl.signal,
        )
      : Promise.resolve(null);

    const moveReq = fetchHistJson(
      `${API}/movement/alerts?date1=${day}&limit=500`,
      ctrl.signal,
    ).catch(() => null);

    const applyMoves = (moveBody: { data?: { items?: Record<string, unknown>[]; status?: string } } | null) => {
      const items = Array.isArray(moveBody?.data?.items) ? moveBody!.data!.items! : [];
      const map: Record<string, Record<string, unknown>> = {};
      for (const it of items) {
        const code = String(it.symbol_code || it.stock_code || "");
        if (code) map[code] = it;
      }
      setMoves(map);
    };

    const loadCum = async () => {
      const [body, subBody, moveBody] = await Promise.all([
        fetchHistJson(
          `${API}/plates/17/${plateCode}/stocks/pct?date1=${day}&days=${pctWindow}`,
          ctrl.signal,
        ) as Promise<{ data?: Record<string, unknown> }>,
        memReq,
        moveReq,
      ]);
      applyMoves(moveBody as { data?: { items?: Record<string, unknown>[] } } | null);
      const data = body?.data || {};
      const intervals: string[] = Array.isArray(data.intervals) && (data.intervals as string[]).length
        ? data.intervals as string[]
        : ["20-40", "40-60", "60-80", "80-100", "100+"];
      const stocks = { ...((data.stocks || {}) as Record<string, Record<string, unknown>[]>) };
      if (subCode) {
        const codes = (subBody as { data?: { stocks?: Record<string, Record<string, string[]>> } })
          ?.data?.stocks?.[day]?.[subCode];
        if (!Array.isArray(codes)) {
          setBuckets({});
          setNote("该二级当日无成分");
          return;
        }
        const allow = new Set(codes.map(String));
        for (const k of Object.keys(stocks)) {
          const arr = Array.isArray(stocks[k]) ? stocks[k] : [];
          stocks[k] = arr.filter((row) => allow.has(String(row.stock_code)));
        }
      }
      setOrder([...intervals].reverse());
      setBuckets(stocks);
      const minPct = Number((data.meta as { min_pct?: number } | undefined)?.min_pct);
      const gate = Number.isFinite(minPct) ? minPct : 20;
      const n = Object.values(stocks).reduce((s, a) => s + (Array.isArray(a) ? a.length : 0), 0);
      if (n) setNote(`≥${gate}% · ${n}只`);
      else setNote(live ? `无≥${gate}%（今日可用盘中价累计；仍空则成分/基准不足）` : `无≥${gate}%`);
    };

    const loadDay = async () => {
      const [subBody, moveBody] = await Promise.all([memReq, moveReq]);
      applyMoves(moveBody);
      let allow: Set<string> | null = null;
      if (subCode) {
        const codes = subBody?.data?.stocks?.[day]?.[subCode];
        if (!Array.isArray(codes)) {
          setBuckets({});
          setNote("该二级当日无成分");
          return;
        }
        allow = new Set(codes.map(String));
      }
      const out: Record<string, Record<string, unknown>[]> = {
        "20+": [], "15-20": [], "10-15": [], "7-10": [], "5-7": [],
      };
      let page = 1;
      while (page <= 20) {
        const url =
          `${API}/plates/17/${plateCode}/stocks/rates?date1=${day}&page=${page}&limit=200`
          + (live ? "&is_real=1" : "");
        const body = await fetch(url, { signal: ctrl.signal }).then((r) => r.json());
        const list = Array.isArray(body?.data?.list) ? body.data.list as Record<string, unknown>[] : [];
        if (!list.length) break;
        let stop = false;
        for (const row of list) {
          const code = String(row.stock_code || "");
          if (allow && !allow.has(code)) continue;
          const rate = Number(row.px_change_rate);
          if (!Number.isFinite(rate)) continue;
          if (rate < 5) {
            stop = true;
            break;
          }
          const iv = dayPctBucket(rate);
          if (!iv) continue;
          out[iv].push({
            stock_code: code,
            stock_name: row.stock_name,
            cum_pct: rate,
          });
        }
        if (stop || list.length < 200) break;
        page += 1;
      }
      for (const arr of Object.values(out)) {
        arr.sort((a, b) => Number(b.cum_pct) - Number(a.cum_pct)
          || String(a.stock_code).localeCompare(String(b.stock_code)));
      }
      setOrder(DAY_PCT_ORDER);
      setBuckets(out);
      const n = Object.values(out).reduce((s, a) => s + a.length, 0);
      setNote(n ? `≥5% · ${n}只` : "无≥5%");
    };

    (pctScope === "day" ? loadDay() : loadCum()).catch((e: { name?: string }) => {
      if (e?.name !== "AbortError") setNote("读取失败");
    });
    return () => ctrl.abort();
  }, [plateCode, subCode, day, pctWindow, pctScope, live]);

  return (
    <div className="flex h-72 flex-col overflow-hidden rounded-md border border-border/50 bg-card/40">
      <div className="shrink-0 border-b border-border/40 px-1.5 py-1">
        <div className="flex items-baseline justify-between gap-1">
          <span className="text-[12px] font-medium tabular-nums tracking-tight">{day.slice(5)}</span>
          <span className="text-[9px] text-muted-foreground">
            {pctScope === "day" ? "当日" : `${pctWindow}日`}
          </span>
        </div>
        {note && (
          <div className="mt-0.5 truncate text-[9px] leading-tight text-muted-foreground" title={note}>
            {note}
          </div>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {order.map((b) => {
          const list = buckets[b] || [];
          return (
            <section key={b} className="border-b border-border/25 last:border-b-0">
              <div className="sticky top-0 z-[1] flex items-center justify-between bg-muted/70 px-1.5 py-0.5 text-[10px] backdrop-blur-sm">
                <span className="font-medium text-foreground/80">{pctBucketLabel(b)}</span>
                <span className="tabular-nums text-muted-foreground">{list.length || "—"}</span>
              </div>
              {list.map((row, i) => {
                const name = String(row.stock_name || row.stock_code || "—");
                const code = String(row.stock_code || "");
                const pct = row.cum_pct != null && Number.isFinite(Number(row.cum_pct))
                  ? `${Number(row.cum_pct) > 0 ? "+" : ""}${Number(row.cum_pct).toFixed(1)}%`
                  : "—";
                const hint = moveHintOf(moves[code]);
                return (
                  <div
                    key={`${code}-${i}`}
                    className="grid grid-cols-[minmax(0,1fr)_1.7rem_2.7rem] items-center gap-x-0.5 px-1.5 py-0 text-[11px] leading-4 hover:bg-muted/35"
                    title={[name, code, hint?.tip].filter(Boolean).join(" · ")}
                  >
                    <span className="truncate text-foreground/90">{name}</span>
                    <span
                      className={cn(
                        "truncate text-right text-[9px] font-medium tabular-nums",
                        hint ? hint.tone : "text-transparent",
                      )}
                    >
                      {hint?.label || "·"}
                    </span>
                    <span className={cn("text-right tabular-nums font-medium", pctTone(row.cum_pct))}>
                      {pct}
                    </span>
                  </div>
                );
              })}
            </section>
          );
        })}
      </div>
    </div>
  );
}

type KlineRow = {
  date: string;
  open: number | null;
  close: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  amount: number | null;
};

function isoDay(v: unknown): string {
  const s = String(v || "");
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  return s.slice(0, 10);
}

function finite(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function maSeries(values: (number | null)[], n: number): (number | null)[] {
  return values.map((_, i) => {
    if (i < n - 1) return null;
    let sum = 0;
    for (let j = i - n + 1; j <= i; j++) {
      const v = values[j];
      if (v == null) return null;
      sum += v;
    }
    return sum / n;
  });
}

function normalizeKlinePayload(raw: unknown): { rows: KlineRow[]; meta: Record<string, unknown> } {
  const source = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const xs = Array.isArray(source.x) ? source.x : [];
  const ys = Array.isArray(source.y) ? source.y : [];
  const vols = Array.isArray(source.vol) ? source.vol : [];
  const amounts = Array.isArray(source.amount)
    ? source.amount
    : Array.isArray(source.turnover) ? source.turnover : [];
  const kind = String(source.series_kind || "");
  const nav = kind === "daily_nav" || kind === "nav";
  const rows: KlineRow[] = [];
  for (let i = 0; i < xs.length; i++) {
    const yi = ys[i];
    if (nav) {
      const close = finite(Array.isArray(yi) ? yi[1] ?? yi[0] : yi);
      rows.push({
        date: isoDay(xs[i]),
        open: close, close, high: close, low: close,
        volume: finite(vols[i]),
        amount: finite(amounts[i]),
      });
      continue;
    }
    const y = Array.isArray(yi) ? yi as unknown[] : [];
    rows.push({
      date: isoDay(xs[i]),
      open: finite(y[0]),
      close: finite(y[1]),
      high: finite(y[2]),
      low: finite(y[3]),
      volume: finite(vols[i]),
      amount: finite(amounts[i]),
    });
  }
  rows.sort((a, b) => a.date.localeCompare(b.date));
  return { rows, meta: source };
}

function KlinePane({
  plateCode, subCode, asOf,
}: {
  plateCode: string;
  subCode: string | null;
  asOf: string;
}) {
  const [opt, setOpt] = useState<EChartsCoreOption | null>(null);
  const [note, setNote] = useState("读取…");
  const [reasons, setReasons] = useState<Record<string, unknown>[]>([]);
  const [reasonNote, setReasonNote] = useState("读取…");
  const [bodies, setBodies] = useState<Record<string, string>>({});

  useEffect(() => {
    const ctrl = new AbortController();
    setOpt(null);
    setNote("读取…");
    // 统一走题材日线契约：一级 main / 二级 sub；真实 OHLC 或显式 daily_nav。
    const kurl = subCode
      ? `${API}/kline/theme/${encodeURIComponent(subCode)}?kind=sub&n=250`
      : `${API}/kline/theme/${encodeURIComponent(plateCode)}?kind=main&n=250`;
    fetch(kurl, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((body) => {
        const { rows, meta } = normalizeKlinePayload(body?.data);
        const isNav = ["nav", "daily_nav"].includes(String(meta.series_kind || ""));
        const usable = isNav
          ? rows.filter((r) => r.close != null)
          : rows.filter((r) => r.open != null && r.close != null && r.high != null && r.low != null);
        if (!usable.length) {
          setOpt(null);
          const why = String(meta.status || meta.reason || "");
          setNote(why.includes("unavailable") ? "K线不可用（缺公开映射/校准源）"
            : why.includes("missing") ? "暂无日净值（缺成分或收盘）" : "暂无日线");
          return;
        }
        // 横轴最新在左（与题材榜列同序）；均线仍按时间正序算再翻转
        const closesAsc = usable.map((r) => r.close);
        const ma5Asc = maSeries(closesAsc, 5);
        const ma25Asc = maSeries(closesAsc, 25);
        const view = usable.slice().reverse();
        const closes = closesAsc.slice().reverse();
        const ma5 = ma5Asc.slice().reverse();
        const ma25 = ma25Asc.slice().reverse();
        const barVals = view.map((r) => r.amount ?? r.volume);
        const hasAmount = view.some((r) => r.amount != null);
        const latest = view[0]?.date || "—";
        const priceName = isNav ? "日净值" : "日K";
        const ma5Name = "5日均";
        const ma25Name = "25日均";
        const barName = hasAmount ? "成交额" : "成交量";
        const zoomEnd = Math.min(100, (60 / Math.max(view.length, 1)) * 100);
        const srcLabel = klineSourceLabel(meta.source);
        setNote(
          `${subCode ? `二级 ${subCode}` : `一级 ${plateCode}`} · ${isNav ? "日净值" : "日线"}截至 ${latest}`
          + (isNav && !srcLabel ? " · 成分等权" : "")
          + (srcLabel ? ` · ${srcLabel}` : ""),
        );
        const priceSeries = isNav
          ? {
            name: priceName,
            type: "line" as const,
            showSymbol: false,
            connectNulls: false,
            data: closes,
            lineStyle: { width: 1.6, color: "#f59e0b" },
          }
          : {
            name: priceName,
            type: "candlestick" as const,
            data: view.map((r) => [r.open!, r.close!, r.low!, r.high!]),
            itemStyle: {
              color: "#ef4444", color0: "#10b981",
              borderColor: "#ef4444", borderColor0: "#10b981",
            },
          };
        const klineTooltip = (params: unknown) => {
          const list = Array.isArray(params) ? params : [params];
          const date = String((list[0] as { axisValue?: string } | undefined)?.axisValue || "");
          const idx = view.findIndex((r) => r.date === date);
          const row = idx >= 0 ? view[idx] : null;
          const lines = [`<b>${date}</b>`];
          if (isNav) {
            lines.push(`净值：${fmtPrice(row?.close ?? closes[idx])}`);
          } else if (row) {
            const chg = row.open != null && row.open !== 0 && row.close != null
              ? ((row.close - row.open) / row.open) * 100
              : null;
            lines.push(`开盘：${fmtPrice(row.open)}`);
            lines.push(`收盘：${fmtPrice(row.close)}`);
            lines.push(`最高：${fmtPrice(row.high)}`);
            lines.push(`最低：${fmtPrice(row.low)}`);
            if (chg != null) lines.push(`涨跌：${chg > 0 ? "+" : ""}${chg.toFixed(2)}%`);
          }
          if (idx >= 0 && ma5[idx] != null) lines.push(`${ma5Name}：${fmtPrice(ma5[idx])}`);
          if (idx >= 0 && ma25[idx] != null) lines.push(`${ma25Name}：${fmtPrice(ma25[idx])}`);
          if (!isNav && row) {
            if (hasAmount) lines.push(`${barName}：${compactAmount(row.amount)}`);
            else if (row.volume != null) lines.push(`${barName}：${compactVolume(row.volume)}`);
          }
          return lines.join("<br/>");
        };
        setOpt({
          animation: false,
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "cross" },
            formatter: klineTooltip,
          },
          legend: {
            data: isNav ? [priceName, ma5Name, ma25Name] : [priceName, ma5Name, ma25Name, barName],
            top: 0, textStyle: { fontSize: 10 },
          },
          axisPointer: {
            link: [{ xAxisIndex: "all" }],
            label: {
              formatter: (p: { axisDimension?: string; value?: unknown }) => {
                if (p.axisDimension === "x") return String(p.value ?? "");
                const n = Number(p.value);
                if (!Number.isFinite(n)) return "";
                // 成交额轴数值通常很大
                return Math.abs(n) >= 1e6 ? compactAmount(n) : fmtPrice(n);
              },
            },
          },
          grid: isNav
            ? [{ left: 52, right: 12, top: 28, bottom: 36 }]
            : [
              { left: 52, right: 12, top: 28, height: "52%" },
              { left: 52, right: 12, top: "72%", height: "16%" },
            ],
          xAxis: isNav
            ? [{ type: "category", data: view.map((r) => r.date), boundaryGap: false, axisLabel: { fontSize: 9 } }]
            : [
              { type: "category", data: view.map((r) => r.date), boundaryGap: true, axisLabel: { fontSize: 9 } },
              { type: "category", gridIndex: 1, data: view.map((r) => r.date), axisLabel: { show: false } },
            ],
          yAxis: isNav
            ? [{
              scale: true, splitNumber: 4,
              axisLabel: { fontSize: 9, formatter: (v: number) => fmtPrice(v) },
            }]
            : [
              {
                scale: true, splitNumber: 4,
                axisLabel: { fontSize: 9, formatter: (v: number) => fmtPrice(v) },
              },
              {
                scale: true, gridIndex: 1, splitNumber: 2,
                axisLabel: {
                  fontSize: 9, show: true, showMinLabel: false, showMaxLabel: false,
                  formatter: (v: number) => (hasAmount ? compactAmount(v) : compactVolume(v)),
                },
              },
            ],
          dataZoom: [
            {
              type: "inside",
              xAxisIndex: isNav ? [0] : [0, 1],
              start: 0,
              end: zoomEnd,
            },
            {
              type: "slider",
              xAxisIndex: isNav ? [0] : [0, 1],
              bottom: 2, height: 14,
              start: 0,
              end: zoomEnd,
            },
          ],
          series: isNav
            ? [
              priceSeries,
              { name: ma5Name, type: "line", showSymbol: false, data: ma5, lineStyle: { width: 1.2, color: "#d89d22" } },
              { name: ma25Name, type: "line", showSymbol: false, data: ma25, lineStyle: { width: 1.2, color: "#7c3aed" } },
            ]
            : [
              priceSeries,
              { name: ma5Name, type: "line", showSymbol: false, data: ma5, lineStyle: { width: 1.2, color: "#d89d22" } },
              { name: ma25Name, type: "line", showSymbol: false, data: ma25, lineStyle: { width: 1.2, color: "#7c3aed" } },
              {
                name: barName,
                type: "bar",
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: view.map((r, i) => {
                  const v = barVals[i];
                  if (v == null) return null;
                  const up = (r.close ?? 0) >= (r.open ?? 0);
                  return { value: v, itemStyle: { color: up ? "#ef4444" : "#10b981" } };
                }),
              },
            ],
        });
      })
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError") setNote("K线失败");
      });
    return () => ctrl.abort();
  }, [plateCode, subCode]);

  useEffect(() => {
    const ctrl = new AbortController();
    setReasons([]);
    setBodies({});
    setReasonNote("读取…");
    fetch(
      `${API}/plate/popular/reason?plate_code=${encodeURIComponent(plateCode)}&limit=30`,
      { signal: ctrl.signal },
    )
      .then((r) => r.json())
      .then(async (body) => {
        if (ctrl.signal.aborted) return;
        const list = Array.isArray(body?.data) ? body.data as Record<string, unknown>[]
          : Array.isArray(body) ? body as Record<string, unknown>[] : [];
        const rows = list.filter((row) => {
          const day = isoDay(row.date);
          return /^\d{4}-\d{2}-\d{2}$/.test(day) && (!asOf || day <= asOf);
        });
        rows.sort((a, b) => isoDay(b.date).localeCompare(isoDay(a.date)));
        setReasons(rows);
        setReasonNote(rows.length ? `${rows.length} 条 · 按交易日` : "暂无驱动消息");

        const next: Record<string, string> = {};
        await Promise.all(rows.map(async (row, i) => {
          const key = String(row.newid || row.msg_id || row.id || i);
          const embedded = reasonBodyText(row);
          if (embedded) {
            next[key] = embedded;
            return;
          }
          try {
            const res = await fetch(
              `${API}/plate/popular/reason/content?msgid=${encodeURIComponent(key)}`,
              { signal: ctrl.signal },
            ).then((r) => r.json());
            if (ctrl.signal.aborted) return;
            const p = (res?.data || {}) as Record<string, unknown>;
            const text = htmlToPlain(String(p.Content || p.content || ""))
              || String(p.Title || p.title || row.boom_reason || row.title || "");
            next[key] = text || "暂无正文";
          } catch (e: unknown) {
            if ((e as { name?: string })?.name === "AbortError") return;
            next[key] = String(row.boom_reason || row.boomreason || row.title || "暂无正文");
          }
        }));
        if (!ctrl.signal.aborted) setBodies(next);
      })
      .catch((e: { name?: string }) => {
        if (e?.name !== "AbortError") setReasonNote("驱动消息加载失败");
      });
    return () => ctrl.abort();
  }, [plateCode, asOf]);

  const reasonGroups = useMemo(() => {
    const map = new Map<string, Record<string, unknown>[]>();
    for (const row of reasons) {
      const day = isoDay(row.date) || "未知日期";
      const arr = map.get(day) || [];
      arr.push(row);
      map.set(day, arr);
    }
    return [...map.entries()];
  }, [reasons]);

  return (
    <div className="space-y-3">
      <div>
        <p className="mb-1 text-[11px] text-muted-foreground">{note}</p>
        {opt ? <EChart option={opt} height={280} /> : null}
      </div>
      <div className="rounded-md border border-border/50 bg-card/40">
        <div className="flex items-center justify-between border-b border-border/40 px-2 py-1.5">
          <div className="min-w-0">
            <span className="text-[12px] font-medium">板块驱动消息</span>
            <span className="ml-1.5 text-[10px] text-muted-foreground">催化/事件，非涨跌归因结论</span>
          </div>
          <span className="shrink-0 text-[10px] text-muted-foreground">{reasonNote}</span>
        </div>
        <div className="max-h-72 space-y-3 overflow-y-auto p-2">
          {reasonGroups.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">{reasonNote}</p>
          ) : reasonGroups.map(([day, items]) => (
            <section key={day}>
              <div className="sticky top-0 z-[1] mb-1 bg-card/95 px-0.5 py-0.5 text-[11px] font-medium tabular-nums text-foreground/80 backdrop-blur-sm">
                {day}
                <span className="ml-1 font-normal text-muted-foreground">{items.length} 条</span>
              </div>
              <div className="space-y-2">
                {items.map((r, i) => {
                  const key = String(r.newid || r.msg_id || r.id || `${day}-${i}`);
                  const title = String(r.boom_reason || r.boomreason || r.title || "未命名消息");
                  const boom = Number(r.is_boom ?? r.isboom) === 1;
                  const zt = r.zt_num ?? r.ztnum;
                  const qd = r.qd;
                  const body = bodies[key]
                    || reasonBodyText(r)
                    || String(r.title || r.boom_reason || "");
                  return (
                    <article key={key} className="rounded border border-border/30 px-2 py-1.5 text-[11px]">
                      <div className="flex items-start justify-between gap-2">
                        <b className="min-w-0 text-foreground/90">
                          {boom ? <span className="mr-1 rounded bg-primary/15 px-1 text-[10px] font-medium text-primary">主因</span> : null}
                          {title}
                        </b>
                        <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground">
                          {zt != null && zt !== "" ? `涨停 ${zt}` : ""}
                          {qd != null && qd !== "" ? `${zt != null && zt !== "" ? " · " : ""}强度 ${qd}` : ""}
                        </span>
                      </div>
                      {body ? (
                        <p className="mt-1 whitespace-pre-wrap leading-relaxed text-muted-foreground">{body}</p>
                      ) : (
                        <p className="mt-1 text-muted-foreground">正文加载中…</p>
                      )}
                    </article>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

function htmlToPlain(html: string): string {
  const s = String(html || "");
  if (!s.trim()) return "";
  if (typeof document !== "undefined") {
    const el = document.createElement("div");
    el.innerHTML = s;
    return (el.textContent || el.innerText || "").replace(/\u200b/g, "").trim();
  }
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

/** 列表项内嵌 content（原站 Python repr / 已解析对象）→ 纯文本正文。 */
function reasonBodyText(row: Record<string, unknown>): string {
  const raw = row.content;
  if (!raw) return "";
  if (typeof raw === "object" && raw && !Array.isArray(raw)) {
    const obj = raw as Record<string, unknown>;
    return htmlToPlain(String(obj.Content || obj.content || ""));
  }
  if (typeof raw !== "string" || !raw.trim()) return "";
  // 优先抽取 Content 字段，避免整段 repr 误当正文
  const m = raw.match(/['"]Content['"]\s*:\s*'((?:\\'|[^'])*)'/)
    || raw.match(/['"]Content['"]\s*:\s*"((?:\\"|[^"])*)"/);
  if (m?.[1]) {
    const html = m[1]
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
      .replace(/\\'/g, "'")
      .replace(/\\"/g, '"')
      .replace(/\\n/g, "\n");
    return htmlToPlain(html);
  }
  if (raw.includes("<p>") || raw.includes("<div")) return htmlToPlain(raw);
  return "";
}

function Check({ on, set, label }: { on: boolean; set: (v: boolean) => void; label: string }) {
  return (
    <label className="inline-flex items-center gap-1 text-muted-foreground">
      <input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} />
      {label}
    </label>
  );
}

function Select<T extends number>({ label, value, set, options }: { label: string; value: T; set: (v: T) => void; options: T[] }) {
  return (
    <label className="inline-flex items-center gap-1 text-muted-foreground">
      {label}
      <select value={value} onChange={(e) => set(Number(e.target.value) as T)}
        className="rounded border border-border bg-transparent px-1.5 py-1">
        {options.map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
    </label>
  );
}
