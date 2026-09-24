import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, backend, friendlyAgentError } from "../src/verticals/finance/lib/backend.ts";

test("未接入 AI 仍可调用确定性工具，并传递取消信号", async () => {
  const oldFetch = globalThis.fetch;
  const oldStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => null } });
  const signal = new AbortController().signal;
  let calls = 0;
  globalThis.fetch = async (input, init) => {
    calls++;
    assert.equal(String(input), "/api/tool/calc");
    assert.equal(init?.signal, signal);
    const body = JSON.parse(String(init?.body));
    assert.equal(body.executionMode, "direct");
    assert.equal(body.llm, undefined);
    return new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json" } });
  };
  try {
    assert.deepEqual(await backend.runTool("calc", { function: "peg" }, signal), { ok: true });
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = oldFetch;
    if (oldStorage) Object.defineProperty(globalThis, "localStorage", oldStorage);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});

test("Agent 认证失败只显示可行动的中文提示，不暴露重连地址与鉴权原文", () => {
  const raw = new ApiError(
    "Reconnecting... 2/5 (unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, url: wss://api.openai.com/v1/responses, cf-ray: secret)",
    400,
    "turn_failed",
  );
  const shown = friendlyAgentError(raw);
  assert.equal(shown, "当前 AI 登录已失效或尚未完成。请先到「接入 AI」重新连接。");
  assert.doesNotMatch(shown, /401|Unauthorized|Missing bearer|wss?:\/\/|cf-ray|Reconnecting/i);
});

test("选中的本地 Agent 不可用时先拦住，不再发起对话请求", async () => {
  const oldFetch = globalThis.fetch;
  const oldStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => JSON.stringify({
    schemaVersion: 2, modePreferenceVersion: 1, executionMode: "agent", source: { provider: "cli-codex" },
  }) } });
  const paths: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    paths.push(url);
    if (url === "/api/local-agents?provider=cli-codex") {
      return new Response(JSON.stringify([{
        provider: "cli-codex", name: "Codex", installed: true, authenticated: false,
        available: false, version: "0.149.0", status: "not_authenticated", detail: "尚未登录",
      }]), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    throw new Error(`不应请求 ${url}`);
  }) as typeof fetch;
  try {
    await assert.rejects(
      () => backend.chat("复盘今天", "daily-review", undefined, { provider: "cli-codex" }),
      (e: unknown) => e instanceof ApiError && e.code === "agent_not_ready"
        && e.message.includes("接入 AI"),
    );
    assert.deepEqual(paths, ["/api/local-agents?provider=cli-codex"], "只检测所选来源，不等待其他来源，也不调用 /chat");
  } finally {
    globalThis.fetch = oldFetch;
    if (oldStorage) Object.defineProperty(globalThis, "localStorage", oldStorage);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});

test("检测失败与未安装不再被误报成登录失效", () => {
  assert.match(friendlyAgentError(new ApiError("opaque secret diagnostic", 409, "agent_probe_failed")), /检测未完成/);
  assert.doesNotMatch(friendlyAgentError(new ApiError("opaque secret diagnostic", 409, "agent_probe_failed")), /secret|登录已失效/);
  assert.match(friendlyAgentError(new ApiError("当前选择的 Claude 尚未安装。请先到「接入 AI」完成连接。", 409, "agent_not_installed")), /尚未安装/);
});

test("Agent 端点返回非 JSON 错误页时也不回显底层正文", async () => {
  const oldFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(
    "502 upstream: Reconnecting wss://api.openai.com/v1/responses cf-ray: secret",
    { status: 502, headers: { "Content-Type": "text/html" } },
  )) as typeof fetch;
  try {
    await assert.rejects(
      () => backend.chat("复盘今天", "daily-review", undefined, { provider: "deepseek", apiKey: "k" }),
      (e: unknown) => e instanceof ApiError
        && e.message === "当前 AI 暂时没有连接成功。请到「接入 AI」检查当前连接后重试。"
        && !/wss?:\/\/|cf-ray|Reconnecting|502 upstream/i.test(e.message),
    );
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("Agent 端点返回 JSON 错误时默认不透传鉴权头、普通上游地址或 HTML", async () => {
  const oldFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    error: "turn_failed",
    message: "request headers: Authorization: Bearer secret; upstream https://example.com <html>bad gateway</html>",
  }), { status: 502, headers: { "Content-Type": "application/json" } })) as typeof fetch;
  try {
    await assert.rejects(
      () => backend.chat("复盘今天", "daily-review", undefined, { provider: "deepseek", apiKey: "k" }),
      (e: unknown) => e instanceof ApiError
        && e.message === "当前 AI 登录已失效或尚未完成。请先到「接入 AI」重新连接。"
        && !/authorization|bearer|https?:\/\/|<html|bad gateway/i.test(e.message),
    );
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("Agent 未知 JSON 错误也默认收口，不把内部诊断原文交给页面", async () => {
  const oldFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    error: "turn_failed", message: "opaque internal diagnostic secret-value",
  }), { status: 500, headers: { "Content-Type": "application/json" } })) as typeof fetch;
  try {
    await assert.rejects(
      () => backend.chat("复盘今天", "daily-review", undefined, { provider: "deepseek", apiKey: "k" }),
      (e: unknown) => e instanceof ApiError
        && e.code === "turn_failed"
        && e.message === "当前 AI 暂时没有连接成功。请到「接入 AI」检查当前连接后重试。"
        && !/opaque|diagnostic|secret-value/i.test(e.message),
    );
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("不支持的 cli provider 交给后端判定，不误报成未登录", async () => {
  const oldFetch = globalThis.fetch;
  const paths: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    paths.push(url);
    if (url === "/api/chat") {
      return new Response(JSON.stringify({ error: "unsupported_cli", message: "订阅档当前只支持 Codex 与 Claude Code" }), {
        status: 400, headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`不应请求 ${url}`);
  }) as typeof fetch;
  try {
    await assert.rejects(
      () => backend.chat("复盘今天", "daily-review", undefined, { provider: "cli-unknown" }),
      (e: unknown) => e instanceof ApiError && e.code === "unsupported_cli"
        && e.message.includes("只支持 Codex 与 Claude Code"),
    );
    assert.deepEqual(paths, ["/api/chat"]);
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("持仓转写的文件和草稿错误不能说成 AI 没连接", () => {
  const cases = [
    ["a.csv 声称是文本,内容却不是合法 UTF-8", "content_mismatch"],
    ["请求体过大", "body_too_large"],
    ["模型没有给出可用的核对草稿。请重试，或换一份更清晰的截图；表格请另存为 UTF-8 的 CSV。", "ingest_turn_failed"],
  ] as const;
  for (const [message, code] of cases) {
    assert.equal(friendlyAgentError(new ApiError(message, 400, code)), message);
  }
});

test("连接探针「有响应但没回填令牌」是可行动错误，原样显示而不是当成连接失败（#40）", () => {
  const raw = new ApiError("模型已响应，但没有按探针格式回填本次令牌。请确认所选模型能遵循结构化输出要求后重试", 400, "probe_bad_output");
  assert.equal(friendlyAgentError(raw), raw.message);
  assert.doesNotMatch(friendlyAgentError(raw), /连接成功|重新连接/);
});
