# ☁️ Cloudflare Worker для ИИ — установка за 10 минут

Этот Worker — «дверь» бота к нейросетям. Он умеет отдавать модели **самого Cloudflare**
(бесплатно, без ключей и без регистрации у Google) и, если захочешь, модели
**Gemini / Groq / OpenRouter** — тогда их ключи лежат внутри Worker'а, а не на твоём ПК.

Что понадобится: почта, браузер, 10 минут. Программы ставить **не нужно** (способ №1).

---

## Способ №1 — через сайт Cloudflare (без установки программ)

### Шаг 1. Аккаунт
1. Открой <https://dash.cloudflare.com/sign-up>
2. Введи почту и пароль → подтверди почту.
3. Карта **не нужна** — бесплатного тарифа достаточно.

### Шаг 2. Создай Worker
1. В левом меню: **Workers & Pages** → **Create** (синяя кнопка) → **Create Worker**
2. Имя можно оставить как есть (`ai-worker`) → **Deploy**
3. Нажми **Edit code** (или «Продолжить работу над проектом»)
4. Удали всё, что там написано, и вставь целиком файл **`worker.js`** из этой папки
5. Нажми **Deploy** ещё раз. Готово — код уже работает.

### Шаг 3. Добавь пароль (секрет)
1. Открой свой Worker → **Settings** → **Variables and Secrets**
2. **Add** → тип **Secret** → имя ровно такое: `PROXY_TOKEN`
3. Значение: придумай длинный пароль, например `bot-3f9a71c2d8` (это пароль между ботом и Worker'ом)
4. **Deploy** / **Save and deploy**

### Шаг 4. Включи бесплатные модели Cloudflare
1. В том же Worker'е → **Settings** → **Bindings** (или **Integrations** в новых версиях панели)
2. **Add binding** → **Workers AI** → имя привязки ровно такое: `AI` → **Save**
3. **Deploy**

> Если биндинга Workers AI в панели нет — не страшно: способ №2 (wrangler) добавляет его одной строкой, либо можно использовать внешних провайдеров из шага 6.

### Шаг 5. Впиши адрес Worker'а в бота
1. Скопируй адрес Worker'а (вида `https://ai-worker.твой-логин.workers.dev`)
2. Открой файл `.env` рядом с `main.py` и добавь две строки:
   ```
   AI_WORKER_URL=https://ai-worker.твой-логин.workers.dev
   AI_WORKER_TOKEN=bot-3f9a71c2d8
   ```
   (токен — тот самый, что ввёл в шаге 3)
3. Проверь: `py main.py --selftest`

### Шаг 6 (необязательно). Ключи Gemini / Groq
Если захочешь модели посерьёзнее, добавь в **Settings → Variables and Secrets** ещё секреты:

| Провайдер | Имя секрета | Откуда взять ключ |
|---|---|---|
| Google Gemini | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> (нужен аккаунт Google на «правильную» страну + VPN) |
| Groq | `GROQ_API_KEY` | <https://console.groq.com/keys> |
| OpenRouter | `OPENROUTER_API_KEY` | <https://openrouter.ai/keys> |

После этого в бота можно вписать приоритет моделей в `.env`:
```
AI_MODEL_PRIORITY=gemini-3.6-flash,@cf/qwen/qwen3-30b-a3b-fp8,@cf/openai/gpt-oss-120b
```
Бот идёт по списку слева направо: если первая модель получила `429`, он автоматически
переходит к следующей. Если все модели исчерпали лимит, главный цикл ждёт сброса.

Актуальные имена Gemini для этого Worker: `gemini-3.8-flash`, `gemini-3.7-flash`,
`gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite` и
`gemini-3.1-flash-lite`. Для обычных гайдов достаточно начать с `gemini-3.6-flash`
или более новой `gemini-3.8-flash`. Модель `gemini-3.8-flash` — самый свежий стабильный
вариант на момент обновления инструкции.

Ключ Gemini создаётся только из поддерживаемого региона Google. Бесплатные запросы могут
использоваться Google для улучшения моделей, поэтому не отправляй через этот канал секреты
и персональные данные.

---

## Способ №2 — через wrangler (для тех, кто дружит с командной строкой)

```bash
# 1. Установить wrangler (нужен Node.js)
npm install -g wrangler

# 2. Войти в аккаунт Cloudflare
wrangler login

# 3. Из папки worker/ задеплоить
cd worker
wrangler deploy

# 4. Задать секреты
wrangler secret put PROXY_TOKEN
wrangler secret put GEMINI_API_KEY     # необязательно
# То же самое без глобальной установки Wrangler:
# npx --yes wrangler secret put GEMINI_API_KEY
wrangler secret put GROQ_API_KEY       # необязательно
wrangler secret put OPENROUTER_API_KEY # необязательно
```

`wrangler.toml` уже содержит привязку `AI` (Workers AI) и Smart Placement.

---

## Как проверить, что Worker работает

Открой в браузере: `https://ai-worker.твой-логин.workers.dev/health`

Должно показать что-то вроде:
```json
{
  "ok": true,
  "version": "1.1",
  "colo": "DME",
  "providers": { "cloudflare": true, "gemini": false, "groq": false, "openrouter": false },
  "models": ["@cf/qwen/qwen3-30b-a3b-fp8", "@cf/openai/gpt-oss-120b", "@cf/openai/gpt-oss-20b"]
}
```

- `"ok": true` — Worker жив.
- `colo` — дата-центр Cloudflare, который обслуживает запрос (`DME` = Москва, `LED` = Санкт-Петербург и т.д.).
- `providers.cloudflare: true` — binding Workers AI подключён.
- `providers.cloudflare: false` и поле `hint` — binding `AI` не подключён, бесплатные модели пока недоступны.
- `providers.gemini: false` — ключ Gemini не задан (это нормально, если он тебе не нужен).

Проверка запроса к модели (подставь свой токен):
```bash
curl -X POST https://ai-worker.твой-логин.workers.dev/v1/chat/completions \
  -H "Authorization: Bearer bot-3f9a71c2d8" \
  -H "Content-Type: application/json" \
  -d '{"model":"@cf/openai/gpt-oss-120b","messages":[{"role":"user","content":"Привет! Ответь одним предложением."}]}'
```

---

## Сколько это стоит и какие лимиты

- **Бесплатно:** 10 000 «нейронов» в сутки (сброс в 00:00 UTC = 03:00 по Москве). Карта не нужна.
- Расход зависит от модели. Одна «игра» в боте (12 идей + 15 гайдов + переводы) — это примерно:
  - `@cf/openai/gpt-oss-120b` — **4–5 игр в сутки** на бесплатном лимите
  - `@cf/qwen/qwen3-30b-a3b-fp8` — **~9 игр в сутки**
- Если лимит выбран — бот сначала пробует следующую модель из списка приоритета. Если все модели вернули `429`, главный цикл ждёт сброса.
- **Платно:** тариф Workers $5/мес, дальше $0.011 за 1 000 нейронов. Одна игра ≈ 2.4 ₽.

---

## Если что-то не работает

| Симптом | Причина | Что делать |
|---|---|---|
| `/health` не открывается, страница обрывается | Провайдер режет Cloudflare (в РФ бывает троттлинг) | Проверить без VPN/с VPN; если не открывается совсем — нужен VPS или прокси в `.env` |
| `401 неверный токен` | Токен в `.env` не совпадает с секретом `PROXY_TOKEN` | Сверить обе строки, лишних пробелов быть не должно |
| `500 server_not_configured` | Секрет `PROXY_TOKEN` не задан | Добавить в Settings → Variables and Secrets |
| `no_binding` | Не подключён Workers AI | Settings → Bindings → Workers AI с именем `AI` (или способ №2) |
| `429 ai_limit_reached` | Исчерпаны 10 000 нейронов на сутки | Подождать до 03:00 МСК или подключить платный тариф |
| Модель отвечает, но по-английски | Модель не держит русский | Смени приоритет в `.env`: `AI_MODEL_PRIORITY=gemini-3.6-flash,@cf/qwen/qwen3-30b-a3b-fp8,...` |
| Gemini в `/health` есть, но отвечает ошибкой региона | Google отказал по гео (Worker выполняется в РФ — `colo: DME/LED`) | Включить Smart Placement (есть в `wrangler.toml`) или ходить через Groq |
