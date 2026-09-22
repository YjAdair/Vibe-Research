import { useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation, useNavigation } from "react-router-dom";
import {
  Activity, ChevronDown, ChevronsLeft, ChevronsRight, Cog, Cpu, FileText, Flame, FlaskConical, Gauge, Github, Globe, Home, LayoutGrid, Microscope, Menu, X, Moon, Newspaper, NotebookPen, Radar, Rss, Settings, Star, Sun, Swords, Thermometer, TrendingUp, UserRound, Wallet,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { PhoenixTreeLogo } from "@/components/ui/PhoenixTreeLogo";
import { AiPageProvider } from "../../../../core/ai/pageContext";
import { FinanceAiDock } from "@/components/ui/FinanceAiDock";
import { useDarkMode } from "@/hooks/useDarkMode";
import { storageGet, storageSet } from "@/lib/storage";
import { HOT_MODULES } from "@/lib/hotModules";
import { useAiRuntime } from "@/hooks/useAiRuntime";
import { aiConnectionLabel } from "@/lib/aiConnection";
import { AgentToggle } from "@/components/ui/AgentToggle";

const REPO_URL = "https://github.com/simonlin1212/Vibe-Research";
// 作者联系方式
const X_URL = "https://x.com/linsizhen";

const NAV = [
  { to: "/", icon: Home, label: "首页" },
  { to: "/daily-review", icon: Activity, label: "每日复盘" },
  { to: "/intel", icon: Radar, label: "资讯雷达" },
  { to: "/signals", icon: Thermometer, label: "产业信号" },
  { to: "/sectors", icon: LayoutGrid, label: "板块中心" },
  { to: "/research", icon: Microscope, label: "个股研究" },
  { to: "/debate", icon: Swords, label: "多空辩论" },
  { to: "/backtest", icon: FlaskConical, label: "回测" },
  { to: "/watchlist", icon: Star, label: "自选股" },
  { to: "/portfolio", icon: Wallet, label: "我的持仓" },
  { to: "/my-reports", icon: FileText, label: "我的研报" },
  { to: "/notes", icon: NotebookPen, label: "研究记录" },
  { to: "/settings", icon: Settings, label: "接入 AI" },
];

type SideLink = { to: string; icon: typeof Flame; label: string; views?: readonly { view: string; label: string }[] };

// 资讯雷达的小栏目（缩进子项，顺序即页内 Tab 顺序）。
const INTEL_LINKS: SideLink[] = [
  { to: "/intel/hot", icon: Flame, label: "热点", views: HOT_MODULES },
  { to: "/intel/investment-news", icon: Rss, label: "Investment News" },
  { to: "/intel/news", icon: Newspaper, label: "公开新闻" },
  { to: "/intel/filings", icon: FileText, label: "A股公告" },
  { to: "/intel/events", icon: TrendingUp, label: "事件概率" },
];

// 产业信号的小栏目（缩进子项，逐期在此添加；带小三角可展开收起）。
const SIGNAL_LINKS = [
  { to: "/signals/gpu-rent", icon: Gauge, label: "GPU租金" },
];

// 常看的板块，作为「板块中心」下的快捷入口（缩进显示）。
// 板块中心下的快捷入口。🔴 只放**环节已核实**的那些 —— 指向空页面的入口比没有入口更糟:
// 用户点进去看到一片空白,分不清是"还没做"还是"坏了"。要加先把环节核实了。
const SECTOR_LINKS = [
  { to: "/sectors/humanoid", icon: Cog, label: "人形机器人" },
  { to: "/sectors/ai-computing", icon: Cpu, label: "AI 算力" },
];

// 带子栏目的导航组：父项右侧小三角展开/收起，展开状态按组记忆。
// 带子栏目的导航组。
// 🔴 存储键**带版本号**：默认值从"展开"改成"收起"时，老键里存着的 "open"
//    会让已经用过的人照旧全展开 —— 那不是 bug（它在记住你的选择），但新默认就等于没生效。
//    换个键 = 旧记忆不再适用，所有人重新从收起开始；之后手动展开的仍然会被记住。
const NAV_GROUPS: Record<string, { storageKey: string; links: readonly SideLink[] }> = {
  "/intel": { storageKey: "vr-intel-open2", links: INTEL_LINKS },
  "/signals": { storageKey: "vr-signals-open2", links: SIGNAL_LINKS },
  "/sectors": { storageKey: "vr-sectors-open2", links: SECTOR_LINKS },
};

export function Layout() {
  const { pathname, search } = useLocation();
  const navigation = useNavigation();
  const aiRuntime = useAiRuntime();
  const { dark, toggle } = useDarkMode();
  const navRef = useRef<HTMLElement | null>(null);
  const sidebarRef = useRef<HTMLElement | null>(null);
  const menuRef = useRef<HTMLButtonElement | null>(null);
  const mainRef = useRef<HTMLElement | null>(null);
  const [mobile, setMobile] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches);
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeMobileNav = () => {
    setMobileOpen(false);
    // The opener is inert until React commits the closed state.
    requestAnimationFrame(() => menuRef.current?.focus());
  };
  const [collapsed, setCollapsed] = useState(() => storageGet("vr-sidebar") === "collapsed");
  // 各导航组子栏目的展开状态（默认展开；按组记住用户的选择）
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(() =>
    // 🔴 **默认收起**：一打开先看到一层干净的总览，要什么再展开。
    //    默认全展开时侧栏一屏塞十几条，一级栏目反而被子项淹掉。
    //    （`=== "open"` 而不是 `!== "closed"`：没存过就是收起。）
    Object.fromEntries(Object.entries(NAV_GROUPS).map(([path, g]) => [path, storageGet(g.storageKey) === "open"])));

  const toggleGroup = (path: string) => {
    setOpenGroups((prev) => {
      const next = { ...prev, [path]: !prev[path] };
      storageSet(NAV_GROUPS[path]!.storageKey, next[path] ? "open" : "closed");
      return next;
    });
  };

  useEffect(() => {
    storageSet("vr-sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

  // 品牌区与底部链接固定，导航本身会滚动。窗口偏矮时当前页可能刚好落在
  // 可视区外（例如最底部的「接入 AI」只露出一条边）—— 路由变化后把当前项拉回视野。
  useEffect(() => {
    const nav = navRef.current;
    const active = nav?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!nav || !active) return;
    const n = nav.getBoundingClientRect();
    const a = active.getBoundingClientRect();
    const breathingRoom = 8;
    if (a.top < n.top + breathingRoom) nav.scrollTop -= n.top + breathingRoom - a.top;
    if (a.bottom > n.bottom - breathingRoom) nav.scrollTop += a.bottom - (n.bottom - breathingRoom);
  }, [pathname, collapsed]);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 767px)");
    const update = () => {
      setMobile(query.matches);
      setMobileOpen(false);
      if (query.matches && sidebarRef.current?.contains(document.activeElement)) {
        requestAnimationFrame(() => menuRef.current?.focus());
      }
    };
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => { setMobileOpen(false); }, [pathname]);
  useEffect(() => {
    if (!mobileOpen || !mobile) return;
    sidebarRef.current?.querySelector<HTMLElement>("a,button")?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); closeMobileNav(); }
      if (event.key !== "Tab") return;
      const items = [...(sidebarRef.current?.querySelectorAll<HTMLElement>("a,button:not(:disabled)") ?? [])];
      const first = items[0], last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keyboard);
    return () => document.removeEventListener("keydown", keyboard);
  }, [mobile, mobileOpen]);
  const compact = collapsed && !mobile;
  const hotLabel = pathname === "/intel/hot"
    ? (HOT_MODULES.find(m => search === `?view=${m.view}`)?.label ?? "题材轮动")
    : undefined;
  const currentTitle = hotLabel
    ?? NAV.find(n => n.to === pathname)?.label
    ?? Object.values(NAV_GROUPS).flatMap(g => g.links).find(n => n.to === pathname)?.label
    ?? "工作空间";

  return (
    <AiPageProvider>
      <a className="workspace-skip" href="#workspace-main" onClick={e => {
        e.preventDefault(); setMobileOpen(false);
        requestAnimationFrame(() => mainRef.current?.focus());
      }}>跳到内容</a>
      <div className="flex h-dvh overflow-hidden">
        {mobile && mobileOpen && <button className="fixed inset-0 z-40 bg-black/60" tabIndex={-1}
          aria-label="关闭导航遮罩" onClick={closeMobileNav} />}
        <aside ref={sidebarRef} aria-label="产品侧栏" className={cn(
          "workspace-sidebar z-50 flex shrink-0 flex-col",
          mobile ? (mobileOpen ? "fixed inset-y-0 left-0 w-[228px]" : "hidden") : compact ? "w-14" : "w-[228px]",
        )}>
          <div className={cn("border-b border-border", compact ? "p-3" : "px-5 py-4")}>
            <div className="flex items-center justify-between">
              <Link to="/" aria-label="Vibe Research 首页" className="flex items-center gap-2.5">
                <PhoenixTreeLogo className="h-8 w-6 shrink-0 text-primary" />
                {!compact && <span className="workspace-brand text-lg font-semibold tracking-tight">Vibe-<span className="text-primary">Research</span></span>}
              </Link>
              {mobile && <button aria-label="关闭导航" className="p-1" onClick={closeMobileNav}><X className="h-4 w-4" /></button>}
            </div>
            {!compact && <div data-ai-identity className="mt-2 space-y-1">
              <p className="text-[10px] leading-4 text-muted-foreground">本地金融研究 Agent · A股 / 美股 / 港股</p>
                <Link to="/settings" data-testid="ai-runtime-badge" title="查看或更改已保存的 AI 接入" className="flex min-w-0 items-start gap-1 text-[10px] leading-5 text-muted-foreground hover:text-primary">
                  <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-primary" /><span>{aiConnectionLabel(aiRuntime)}</span>
                </Link>
                <AgentToggle showHint />
            </div>}
            {compact && <div className="mt-3 flex justify-center"><AgentToggle compact /></div>}
          </div>
          <nav ref={navRef} aria-label="原产品板块导航" className={cn("min-h-0 flex-1 space-y-0.5 overflow-auto py-3", compact ? "px-1.5" : "px-3")}>
            {NAV.map(({ to, icon: Icon, label }) => {
              const active = pathname === to;
              const group = NAV_GROUPS[to];
              const groupOpen = group ? !!openGroups[to] : false;
              const showGroup = groupOpen || compact || (to === "/intel" && pathname === "/intel/hot");
              return <div key={to}>
                <div className="flex items-center">
                  <Link to={to} aria-label={label} aria-current={active ? "page" : undefined} title={compact ? label : undefined}
                    onClick={() => { if (mobile) { setMobileOpen(false); requestAnimationFrame(() => mainRef.current?.focus()); } }}
                    className={cn("workspace-nav-link flex min-w-0 flex-1 items-center text-[13px] transition-colors",
                      compact ? "justify-center p-2.5" : "gap-3 px-3 py-2.5",
                      active ? "font-medium" : "text-muted-foreground hover:bg-muted/50 hover:text-foreground")}>
                    <Icon className="h-4 w-4 shrink-0" />{!compact && <span>{label}</span>}
                  </Link>
                  {group && !compact && <button type="button" aria-label={`${groupOpen ? "收起" : "展开"}${label}子栏目`}
                    aria-expanded={groupOpen} onClick={() => toggleGroup(to)}
                    className="rounded p-2 text-muted-foreground hover:bg-muted/60">
                    <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", !groupOpen && "-rotate-90")} />
                  </button>}
                </div>
                {group && showGroup && <div className={cn("mt-1 space-y-0.5", !compact && "ml-5 border-l border-border pl-2")}>
                  {group.links.map(({ to: st, icon: SIcon, label: slabel, views }) => <div key={st}>
                    <Link to={st}
                      aria-label={slabel} title={compact ? slabel : undefined} aria-current={pathname === st ? "page" : undefined}
                      onClick={() => { if (mobile) { setMobileOpen(false); requestAnimationFrame(() => mainRef.current?.focus()); } }}
                      className={cn("workspace-nav-link flex items-center text-xs text-muted-foreground hover:bg-muted/40 hover:text-foreground",
                        compact ? "justify-center p-2" : "gap-2 px-2 py-1.5")}>
                      <SIcon className="h-3.5 w-3.5 shrink-0" />{!compact && slabel}
                    </Link>
                    {views && !compact && <div className="ml-4 space-y-0.5 border-l border-border pl-2">
                      {views.map((v) => {
                        const on = pathname === st && (search === `?view=${v.view}` || (v.view === "rotation" && search === ""));
                        return <Link key={v.view} to={`${st}?view=${v.view}`} aria-current={on ? "page" : undefined}
                          onClick={() => { if (mobile) { setMobileOpen(false); requestAnimationFrame(() => mainRef.current?.focus()); } }}
                          className={cn("block rounded px-2 py-1 text-[11px] hover:bg-muted/40 hover:text-foreground",
                            on ? "font-medium text-primary" : "text-muted-foreground")}>{v.label}</Link>;
                      })}
                    </div>}
                  </div>)}
                </div>}
              </div>;
            })}
          </nav>
          <div className={cn("border-t border-border", compact ? "p-1.5" : "p-3")}>
            <div className={cn("flex items-center text-muted-foreground", compact ? "flex-col gap-3" : "justify-between gap-2")}>
              <a href="https://phoenixtree.ai/" target="_blank" rel="noopener noreferrer"
                aria-label="Phoenix Tree AI 官网（新标签页打开）" title="Phoenix Tree AI 官网（新标签页打开）"
                className="flex min-h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded text-xs text-primary hover:bg-muted/50">
                <Globe className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                {!compact && <span>phoenixtree.ai</span>}
              </a>
              <div className={cn("flex gap-2", compact && "flex-col")}>
                <a href={X_URL} target="_blank" rel="noreferrer" aria-label="联系作者" title="联系作者 · X @linsizhen"><UserRound className="h-3.5 w-3.5" /></a>
                <a href={REPO_URL} target="_blank" rel="noreferrer" aria-label="GitHub" title="GitHub"><Github className="h-3.5 w-3.5" /></a>
                {!mobile && <button onClick={() => setCollapsed(!collapsed)} aria-label={compact ? "展开侧栏" : "收起侧栏"} title={compact ? "展开侧栏" : "收起侧栏"}>
                  {compact ? <ChevronsRight className="h-3.5 w-3.5" /> : <ChevronsLeft className="h-3.5 w-3.5" />}
                </button>}
              </div>
            </div>
          </div>
        </aside>
        <div inert={mobile && mobileOpen} className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <header className="workspace-topbar flex h-16 shrink-0 items-center justify-between gap-3 px-4 md:px-8">
            <div className="flex min-w-0 items-center gap-3 text-xs">
              <button ref={menuRef} aria-label="打开导航" onClick={() => setMobileOpen(true)} className="p-1 md:hidden"><Menu className="h-4 w-4" /></button>
              <span className="hidden text-muted-foreground sm:inline">工作空间 /</span><strong className="truncate font-medium">{currentTitle}</strong>
            </div>
            <div className={cn("flex items-center gap-3", pathname !== "/" && "mr-24")}>
              <span className="hidden text-[10px] text-muted-foreground lg:inline">本地金融研究工作台</span>
              <button onClick={toggle} className="rounded p-2 text-muted-foreground hover:bg-muted hover:text-foreground" aria-label={dark ? "切换为浅色" : "切换为深色"}>
                {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </button>
            </div>
          </header>
          <main ref={mainRef} id="workspace-main" tabIndex={-1} className="min-h-0 flex-1 overflow-auto">
            <div className="workspace-content" aria-busy={navigation.state !== "idle"}>
              {navigation.state !== "idle" && <p role="status" className="mb-3 text-sm text-muted-foreground">正在打开页面…</p>}
              <Outlet />
            </div>
          </main>
        </div>
        <div inert={mobile && mobileOpen}>{pathname !== "/" && <FinanceAiDock />}</div>
      </div>
    </AiPageProvider>
  );
}
