/**
 * Cloudflare Worker — единая дверь бота к нейросетям.
 *
 * Что делает:
 *   POST /v1/chat/completions  — совместимо с форматом OpenAI.
 *        model = "@cf/..."            → модель самого Cloudflare (ключи не нужны)
 *        model = "gemini-2.5-flash"   → Google Gemini (нужен GEMINI_API_KEY в секретах)
 *        model = "llama-3.3-70b"      → Groq (нужен GROQ_API_KEY)
 *        model = "openrouter/..."     → OpenRouter (нужен OPENROUTER_API_KEY)
 *   GET  /health               — проверка: жив ли Worker, в каком он дата-центре,
 *                                какие ключи заданы, какие модели доступны.
 *
 * Секреты (Settings → Variables and Secrets, тип «Secret»):
 *   PROXY_TOKEN        — обязательный. Пароль, который знает только бот.
 *   GEMINI_API_KEY     — необязательный.
 *   GROQ_API_KEY       — необязательный.
 *   OPENROUTER_API_KEY — необязательный.
 *
 * Переменные (можно открытым текстом):
 *   EXTRA_MODELS       — список доступных имён моделей Cloudflare через запятую,
 *                        показывается в /health (на работу не влияет).
 */

const VERSION = "1.0";

// Модели Cloudflare, которые имеет смысл ставить в приоритет.
// Полный список: https://developers.cloudflare.com/workers-ai/models/
const DEFAULT_CF_MODELS = [
  "@cf/openai/gpt-oss-120b",
  "@cf/qwen/qwen3-30b-a3b-fp8",
  "@cf/openai/gpt-oss-20b",
];

// Внешние провайдеры: имя модели → { адрес, имя секрета с ключом }
const UPSTREAMS = {
  // Google Gemini (OpenAI-совместимый вход)
  "gemini-2.5-flash": { url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", key: "GEMINI_API_KEY" },
  "gemini-2.5-pro": { url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", key: "GEMINI_API_KEY" },
  "gemini-2.0-flash": { url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", key: "GEMINI_API_KEY" },
  // Groq
  "llama-3.3-70b": { url: "https://api.groq.com/openai/v1/chat/completions", key: "GROQ_API_KEY", remote: "llama-3.3-70b-versatile" },
  "gpt-oss-120b": { url: "https://api.groq.com/openai/v1/chat/completions", key: "GROQ_API_KEY", remote: "openai/gpt-oss-120b" },
  "gpt-oss-20b": { url: "https://api.groq.com/openai/v1/chat/completions", key: "GROQ_API_KEY", remote: "openai/gpt-oss-20b" },
  // OpenRouter — любая модель, если имя начинается с "openrouter/"
};

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    if (url.pathname === "/" || url.pathname === "") {
      return json({ ok: true, service: "ai-worker", version: VERSION, hint: "POST /v1/chat/completions" });
    }

    if (url.pathname === "/health") {
      return json({
        ok: true,
        version: VERSION,
        colo: (request.cf && request.cf.colo) || "unknown",
        country: (request.cf && request.cf.country) || "unknown",
        providers: {
          cloudflare: true,
          gemini: Boolean(env.GEMINI_API_KEY),
          groq: Boolean(env.GROQ_API_KEY),
          openrouter: Boolean(env.OPENROUTER_API_KEY),
        },
        models: availableModels(env),
      });
    }

    if (url.pathname === "/v1/chat/completions") {
      if (request.method !== "POST") {
        return json({ error: { message: "используй POST", type: "method_not_allowed" } }, 405);
      }
      const authError = checkAuth(request, env);
      if (authError) return authError;
      return handleChat(request, env);
    }

    if (url.pathname === "/v1/models") {
      return json({
        object: "list",
        data: availableModels(env).map((id) => ({ id, object: "model", owned_by: "self" })),
      });
    }

    return json({ error: { message: "неизвестный путь: " + url.pathname, type: "not_found" } }, 404);
  },
};

// ─── Авторизация ──────────────────────────────────────────────────────

function checkAuth(request, env) {
  if (!env.PROXY_TOKEN) {
    return json({
      error: {
        message: "на Worker'е не задан секрет PROXY_TOKEN — добавь его в настройках Worker'а",
        type: "server_not_configured",
      },
    }, 500);
  }
  const header = request.headers.get("authorization") || "";
  const token = header.replace(/^Bearer\s+/i, "").trim();
  if (token !== env.PROXY_TOKEN) {
    return json({ error: { message: "неверный токен", type: "unauthorized" } }, 401);
  }
  return null;
}

function availableModels(env) {
  const cf = (env.EXTRA_MODELS || "")
    .split(",")
    .map((m) => m.trim())
    .filter(Boolean);
  const list = [...DEFAULT_CF_MODELS, ...cf];
  for (const [name, cfg] of Object.entries(UPSTREAMS)) {
    if (env[cfg.key]) list.push(name);
  }
  if (env.OPENROUTER_API_KEY) list.push("openrouter/<любая модель>");
  return [...new Set(list)];
}

// ─── Основная логика ──────────────────────────────────────────────────

async function handleChat(request, env) {
  let body;
  try {
    body = await request.json();
  } catch (e) {
    return json({ error: { message: "тело запроса не JSON", type: "bad_request" } }, 400);
  }

  const model = String(body.model || "").trim();
  const messages = body.messages;
  if (!model) return json({ error: { message: "не указана модель (model)", type: "bad_request" } }, 400);
  if (!Array.isArray(messages) || messages.length === 0) {
    return json({ error: { message: "нужен непустой массив messages", type: "bad_request" } }, 400);
  }
  if (body.stream) {
    return json({
      error: { message: "стриминг не поддерживается — убери stream или поставь false", type: "bad_request" },
    }, 400);
  }

  // 1) Модели самого Cloudflare
  if (model.startsWith("@cf/")) {
    return runCloudflareModel(model, body, env);
  }

  // 2) OpenRouter (любая модель)
  if (model.startsWith("openrouter/")) {
    if (!env.OPENROUTER_API_KEY) {
      return json({ error: { message: "OPENROUTER_API_KEY не задан на Worker'е", type: "no_key" } }, 400);
    }
    return proxyUpstream({
      url: "https://openrouter.ai/api/v1/chat/completions",
      key: env.OPENROUTER_API_KEY,
      remoteModel: model.slice("openrouter/".length),
      body,
      headers: { "HTTP-Referer": "https://workers.dev", "X-Title": "funpay-bot" },
    });
  }

  // 3) Остальные внешние провайдеры из списка
  const cfg = UPSTREAMS[model];
  if (!cfg) {
    return json({
      error: {
        message: `модель "${model}" не поддерживается. Доступные: ${availableModels(env).join(", ")}`,
        type: "unknown_model",
      },
    }, 400);
  }
  if (!env[cfg.key]) {
    return json({
      error: { message: `для модели "${model}" нужен секрет ${cfg.key} — добавь его в настройках Worker'а`, type: "no_key" },
    }, 400);
  }
  return proxyUpstream({ url: cfg.url, key: env[cfg.key], remoteModel: cfg.remote || model, body });
}

async function runCloudflareModel(model, body, env) {
  if (!env.AI) {
    return json({
      error: { message: "на Worker'е не подключён Workers AI (binding AI). Смотри worker/README.md", type: "no_binding" },
    }, 500);
  }

  const payload = { messages: body.messages };
  if (body.max_tokens) payload.max_tokens = body.max_tokens;
  if (body.temperature !== undefined) payload.temperature = body.temperature;

  try {
    const result = await env.AI.run(model, payload);
    const text = extractCfText(result);
    if (!text) {
      return json({ error: { message: "модель вернула пустой ответ", type: "empty_response", raw: result } }, 502);
    }
    return json({
      id: "chatcmpl-" + crypto.randomUUID(),
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
      usage: (result && result.usage) || undefined,
    });
  } catch (e) {
    return normalizeAiError(e, model);
  }
}

async function proxyUpstream({ url, key, remoteModel, body, headers = {} }) {
  const payload = {
    model: remoteModel,
    messages: body.messages,
    stream: false,
  };
  if (body.max_tokens) payload.max_tokens = body.max_tokens;
  if (body.temperature !== undefined) payload.temperature = body.temperature;

  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${key}`,
        ...headers,
      },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    return json({ error: { message: "не смог дозвониться до провайдера: " + e.message, type: "upstream_unreachable" } }, 502);
  }

  const text = await response.text();
  const isJson = (response.headers.get("content-type") || "").includes("application/json");

  if (response.ok && isJson) {
    try {
      const data = JSON.parse(text);
      // Возвращаем в формате OpenAI, сохраняя usage
      return json({
        id: data.id || "chatcmpl-" + crypto.randomUUID(),
        object: "chat.completion",
        created: data.created || Math.floor(Date.now() / 1000),
        model: data.model || remoteModel,
        choices: data.choices || [],
        usage: data.usage,
      });
    } catch (e) {
      return json({ error: { message: "провайдер вернул нечитаемый JSON", type: "bad_upstream_response" } }, 502);
    }
  }

  const status = response.status === 429 ? 429 : response.status >= 500 ? 502 : 400;
  const type = response.status === 429 ? "ai_limit_reached" : "upstream_error";
  return json({ error: { message: `провайдер ответил ${response.status}: ${text.slice(0, 500)}`, type } }, status);
}

function extractCfText(result) {
  if (!result) return "";
  if (typeof result === "string") return result;
  if (typeof result.response === "string") return result.response;
  if (result.result && typeof result.result.response === "string") return result.result.response;
  // gpt-oss иногда отдаёт ответ в формате choices
  if (Array.isArray(result.choices) && result.choices[0] && result.choices[0].message) {
    return result.choices[0].message.content || "";
  }
  return "";
}

function normalizeAiError(e, model) {
  const message = (e && (e.message || String(e))) || "неизвестная ошибка";
  const low = message.toLowerCase();

  if (low.includes("neuron") || low.includes("limit") || (e && e.status === 429)) {
    return json({
      error: {
        message: "дневной бесплатный лимит нейронов исчерпан. Сброс в 00:00 UTC (03:00 МСК). Ответ модели: " + message,
        type: "ai_limit_reached",
      },
    }, 429);
  }
  if (low.includes("no such model") || low.includes("not found")) {
    return json({ error: { message: `модель "${model}" недоступна: ${message}`, type: "unknown_model" } }, 400);
  }
  return json({ error: { message: `ошибка модели "${model}": ${message}`, type: "ai_error" } }, 502);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}
