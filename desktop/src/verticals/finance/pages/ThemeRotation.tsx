import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
const API = "http://127.0.0.1:8000/v3/market";
const OPEN = "http://127.0.0.1:8000/v3/open";
const RANK_URL = `${API}/plates/17/rank/columns`;
const TREND_URL = `${API}/plates/17/rank/trend`;
const POLL_MS = 60_000;

export function ThemeRotation() {
  const [showMoney, setShowMoney] = useState(false);
  const [showScore, setShowScore] = useState(true);
  const [showRate, setShowRate] = useState(true);
  const [days, setDays] = useState(15);
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
        <Select label="天数" value={days} set={setDays} options={[5, 10, 15]} />
        <Select label="Top" value={topN} set={setTopN} options={[8, 12, 20]} />
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
    return <KlinePane plateCode={plateCode} subCode={subCode} />;
  }

  const latestDay = dates[0];

  return (
    <div className="flex gap-1.5 overflow-x-auto pb-1">
      {dates.map((day) => (
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
              {mode === "stock" && day === latestDay && (
                <span className="text-[10px] text-primary/80">实时</span>
              )}
            </div>
          )}
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
        </div>
      ))}
    </div>
  );
}

const PAGE_MISS = "缺成分/行情";

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
      const body = await fetch(
        `${API}/plates/${plateType}/${code}/stocks/rates?${q}`,
        { signal },
      ).then((r) => r.json());
      if (signal.aborted || gen !== genRef.current) return;
      const list = Array.isArray(body?.data?.list) ? body.data.list as Record<string, unknown>[] : [];
      const tot = Number(body?.data?.total) || 0;
      setTotal(tot);
      setRows((prev) => (reset ? list : [...prev, ...list]));
      setPage(next);
      setErr(list.length || !reset ? "" : PAGE_MISS);
    } catch (e: unknown) {
      if ((e as { name?: string })?.name !== "AbortError" && gen === genRef.current) {
        setErr("读取失败");
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
    void loadPage(1, ctrl.signal, true);
    return () => ctrl.abort();
  }, [loadPage]);

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
    fetch(url, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((body) => {
        if (gen !== genRef.current) return;
        const list = Array.isArray(body?.data?.list) ? body.data.list as Record<string, unknown>[] : [];
        const tot = Math.min(Number(body?.data?.total) || 0, cap);
        const status = body?.data?.status || body?.data?.meta?.status;
        const note = typeof body?.data?.meta?.note === "string" ? body.data.meta.note : "";
        setTotal(tot);
        setRows(list.slice(0, cap));
        setPage(1);
        if (list.length) {
          setErr(kind === "popular" && note ? note : "");
        } else if (status === "missing_popular_snapshot") {
          setErr(note || "该日无人气快照");
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

    const hot = fetch(
      `${OPEN}/review/uplimit/hot?board=${encodeURIComponent(plateCode)}&date1=${day}`,
      { signal: ctrl.signal },
    ).then((r) => r.json());

    const members = subCode
      ? fetch(
          `${API}/plates/17/${encodeURIComponent(plateCode)}/sub-plates-stocks?dates=${day}`,
          { signal: ctrl.signal },
        ).then((r) => r.json())
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
      ? fetch(
          `${API}/plates/17/${encodeURIComponent(plateCode)}/sub-plates-stocks?dates=${day}`,
          { signal: ctrl.signal },
        ).then((r) => r.json())
      : Promise.resolve(null);

    const moveReq = fetch(
      `${API}/movement/alerts?date1=${day}&limit=500`,
      { signal: ctrl.signal },
    ).then((r) => r.json()).catch(() => null);

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
        fetch(
          `${API}/plates/17/${plateCode}/stocks/pct?date1=${day}&days=${pctWindow}`,
          { signal: ctrl.signal },
        ).then((r) => r.json()),
        memReq,
        moveReq,
      ]);
      applyMoves(moveBody);
      const data = body?.data || {};
      const intervals: string[] = Array.isArray(data.intervals) && data.intervals.length
        ? data.intervals
        : ["20-40", "40-60", "60-80", "80-100", "100+"];
      const stocks = { ...((data.stocks || {}) as Record<string, Record<string, unknown>[]>) };
      if (subCode) {
        const codes = subBody?.data?.stocks?.[day]?.[subCode];
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
      const minPct = Number(data.meta?.min_pct);
      const gate = Number.isFinite(minPct) ? minPct : 20;
      const n = Object.values(stocks).reduce((s, a) => s + (Array.isArray(a) ? a.length : 0), 0);
      setNote(n ? `≥${gate}% · ${n}只` : `无≥${gate}%`);
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

function KlinePane({ plateCode, subCode }: { plateCode: string; subCode: string | null }) {
  const [opt, setOpt] = useState<EChartsCoreOption | null>(null);
  const [note, setNote] = useState("读取…");
  useEffect(() => {
    const ctrl = new AbortController();
    const kurl = subCode
      ? `${API}/kline/sub-plate/${subCode}?n=60`
      : `${API}/kline/plate/${plateCode}?n=60`;
    fetch(kurl, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((body) => {
        const data = body?.data;
        const xs: string[] = data?.x || data?.dates || [];
        const ys = data?.y || data?.ohlc || [];
        const closes = Array.isArray(ys)
          ? ys.map((row: unknown) => (Array.isArray(row) ? num(row[1]) : num(row)))
          : [];
        if (!xs.length || closes.every((v) => v == null)) {
          setOpt(null);
          setNote("K线空（缺 801 指数序列）");
          return;
        }
        setNote(`${subCode ? `二级 ${subCode}` : `一级 ${plateCode}`} · ${xs.length} 根`);
        setOpt({
          animation: false,
          grid: { left: 8, right: 8, top: 12, bottom: 20 },
          xAxis: { type: "category", data: xs, show: false },
          yAxis: { type: "value", show: false, scale: true },
          series: [{ type: "line", data: closes, showSymbol: false, lineStyle: { width: 1, color: "#999" } }],
        });
      })
      .catch((e: { name?: string }) => { if (e?.name !== "AbortError") setNote("K线失败"); });
    return () => ctrl.abort();
  }, [plateCode, subCode]);
  if (!opt) return <p className="text-muted-foreground">{note}</p>;
  return (
    <div>
      <p className="mb-1 text-[11px] text-muted-foreground">{note}</p>
      <EChart option={opt} height={160} />
    </div>
  );
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
