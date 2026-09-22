/** 热点下可切换的子模块。顺序即侧栏与页内顺序。 */
export const HOT_MODULES = [
  { view: "rotation", label: "题材轮动" },
  { view: "tables", label: "题材表格" },
  { view: "mine", label: "我的题材" },
  { view: "preopen", label: "每日盘前(AI)" },
  { view: "postclose", label: "每日盘后(AI)" },
] as const;

export type HotView = (typeof HOT_MODULES)[number]["view"];

export function hotHref(view: HotView) {
  return `/intel/hot?view=${view}`;
}
