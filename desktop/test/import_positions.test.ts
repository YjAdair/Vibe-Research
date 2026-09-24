import assert from "node:assert/strict";
import test from "node:test";
import { encodeImportFiles, positionValues } from "../src/verticals/finance/lib/importPositions.ts";
import { backend } from "../src/verticals/finance/lib/backend.ts";

test("导入保持原始文件字节，提前拒绝超限、格式错误与取消", async () => {
  const signal = new AbortController().signal;
  const [file] = await encodeImportFiles([new File(["代码,数量\n600519,300"], "持仓.csv")], signal);
  assert.equal(Buffer.from(file!.content_base64, "base64").toString(), "代码,数量\n600519,300");
  for (const files of [[], [new File(["x"], "a.xlsx")], [new File([], "a.txt")], Array.from({ length: 11 }, () => new File(["x"], "a.csv")),
    [new File([new Uint8Array(8 * 1024 * 1024 + 1)], "large.txt")]]) await assert.rejects(encodeImportFiles(files, signal));
  const ac = new AbortController(); ac.abort();
  await assert.rejects(encodeImportFiles([new File(["x"], "a.csv")], ac.signal));
});

test("草稿只填代码数量成本，空值不变零，负成本保持，不自动写台账", () => {
  assert.deepEqual(positionValues({ symbol: "600519", shares: 300, cost: -1.23, note: "不会自动写入" }), { symbol: "600519", shares: "300", cost: "-1.23" });
  for (const fields of [{}, { symbol: "600519", shares: null, cost: 0 }, { symbol: "600519", shares: 1, cost: "" },
    { symbol: "600519", shares: Infinity, cost: 1 }]) assert.throws(() => positionValues(fields));
});

test("转写沿用选中来源与 Agent 开关，并传递取消；不会调用台账写入", async () => {
  const originalFetch = globalThis.fetch;
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => JSON.stringify({ schemaVersion: 2, modePreferenceVersion: 1, source: { provider: "cli-codebuddy" }, executionMode: "agent" }) } });
  const signal = new AbortController().signal;
  const calls: string[] = [];
  globalThis.fetch = async (url, init) => {
    const path = String(url);
    calls.push(path);
    assert.equal(init?.signal, signal);
    if (path.startsWith("/api/local-agents")) {
      return new Response(JSON.stringify([{ provider: "cli-codebuddy", name: "CodeBuddy", available: true, status: "ready" }]));
    }
    assert.equal(path, "/api/import");
    const body = JSON.parse(String(init?.body));
    assert.equal(body.llm.provider, "cli-codebuddy"); assert.equal(body.executionMode, "agent"); assert.equal(body.kind, "position");
    return new Response(JSON.stringify({ batch: "b", kind: "position", drafts: [], warnings: [] }));
  };
  try {
    await backend.importPositions([{ name: "a.csv", content_base64: "eA==" }], signal);
    assert.equal(calls.length, 2);
    assert.match(calls[0]!, /\/api\/local-agents/);
    assert.equal(calls[1], "/api/import");
  }
  finally { globalThis.fetch = originalFetch; if (originalStorage) Object.defineProperty(globalThis, "localStorage", originalStorage); else Reflect.deleteProperty(globalThis, "localStorage"); }
});

test("普通对话模式不发起转写，并说明要开启 Agent", async () => {
  const originalFetch = globalThis.fetch;
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => JSON.stringify({ schemaVersion: 2, modePreferenceVersion: 1, source: { provider: "cli-codebuddy" }, executionMode: "direct" }) } });
  globalThis.fetch = async () => { throw new Error("不应请求"); };
  try {
    await assert.rejects(
      () => backend.importPositions([{ name: "a.csv", content_base64: "eA==" }]),
      (e: unknown) => e instanceof Error && e.message.includes("开启 Vibe Research Agent"),
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (originalStorage) Object.defineProperty(globalThis, "localStorage", originalStorage);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});
