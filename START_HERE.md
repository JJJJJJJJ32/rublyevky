# ✅ Чек-лист перед первым запуском

Всё, что нужно сделать один раз. По времени — примерно 20 минут.
Подробности по каждому пункту: [README.md](README.md) и [worker/README.md](worker/README.md).

---

## 0. Что должно быть готово

- [ ] **Python 3.10+** — проверь в PowerShell: `python --version` (или `py --version`)
- [ ] **Аккаунт FunPay** и **Golden Key**: FunPay → Настройки → Золотой ключ
- [ ] **Аккаунт Google** (для папки с гайдами)
- [ ] **Аккаунт Cloudflare** — бесплатно: <https://dash.cloudflare.com/sign-up>
- [ ] **Интернет**, который открывает funpay.com в браузере

---

## 1. Зависимости

```powershell
cd путь\к\папке\с\ботом
py -m pip install -r requirements.txt
```

---

## 2. Файл `.env` (рядом с `main.py`)

- [ ] Скопируй `.env.example` в `.env` и заполни:

```
GOLDEN_KEY=твой_золотой_ключ_от_funpay
GOOGLE_DRIVE_FOLDER_ID=айди_папки_на_гугл_диске
AI_WORKER_URL=https://ai-worker.твой-логин.workers.dev
AI_WORKER_TOKEN=пароль_из_настроек_воркера
AI_MODEL_PRIORITY=@cf/openai/gpt-oss-120b,@cf/qwen/qwen3-30b-a3b-fp8
```

> Файл должен называться именно `.env`, а не `.env.txt` (в блокноте при сохранении выбери «Все файлы»).
> Значения `AI_WORKER_URL` и `AI_WORKER_TOKEN` появятся у тебя в пункте 4.

---

## 3. Google Drive

- [ ] В [Google Cloud Console](https://console.cloud.google.com/) создай проект и включи **Google Drive API**
- [ ] Создай ключ (любой из двух):
  - **Способ А, проще:** Service Account → Keys → Create new key → JSON → переименуй в **`client_secrets.json`** → положи рядом с `main.py`
  - **Способ Б:** OAuth client ID → тип **Desktop app** → Download JSON → тоже переименуй в **`client_secrets.json`**
- [ ] Если выбрал способ А — **поделись папкой** на Google Drive с email сервисного аккаунта (он внутри `client_secrets.json`, поле `client_email`), роль «Редактор»
- [ ] Скопируй ID папки из адреса (`.../folders/`**`вот_это`**) → в строку `GOOGLE_DRIVE_FOLDER_ID` в `.env`

> Бот сам понимает, какой из двух ключей ты положил.

---

## 4. Cloudflare Worker (ИИ)

Пошагово с картинками-описаниями: [worker/README.md](worker/README.md)

- [ ] Зарегистрируйся на <https://dash.cloudflare.com/sign-up> (карта не нужна)
- [ ] **Workers & Pages → Create → Create Worker → Deploy**
- [ ] Вставь код из `worker/worker.js` → **Deploy**
- [ ] **Settings → Variables and Secrets** → добавь **Secret** с именем `PROXY_TOKEN` (придумай длинный пароль) → **Deploy**
- [ ] **Settings → Bindings → Add binding → Workers AI**, имя привязки ровно `AI` → **Deploy**
- [ ] Скопируй адрес Worker'а (вида `https://ai-worker.логин.workers.dev`):
  - [ ] открой в браузере `твой-адрес/health` — должно быть `"ok": true`
  - [ ] впиши адрес и токен в `.env` (пункт 2)

---

## 5. Список игр

- [ ] Открой `data/games_list.txt`, убедись что там твои игры (по одной на строку)
- [ ] ⚠️ **Сделай копию файла** (`games_list_backup.txt`) — бот удаляет игру из списка после обработки

---

## 6. Проверка и запуск

```powershell
py main.py --selftest     # проверка за 30 секунд, ничего не публикует
py main.py                # рабочий запуск
```

`--selftest` должен показать примерно это:

```
[1/5] Файл .env
   ✅ .env найден
   • GOLDEN_KEY: ✅ есть
   • GOOGLE_DRIVE_FOLDER_ID: ✅ есть
   • AI_WORKER_URL: ✅ есть
[2/5] Cloudflare Worker (ИИ)
   ✅ Worker отвечает ... дата-центр Cloudflare: DME
   ✅ ИИ ответил: ...
[3/5] FunPay
   ✅ Авторизация по GOLDEN_KEY прошла
[4/5] Google Drive
   ✅ Диск доступен, папка создаётся и выдаются права на чтение
```

Если всё зелёное — можно запускать `py main.py`.

---

## Что бот сделает при первом запуске

1. Возьмёт **первую игру** из `data/games_list.txt`
2. Найдёт её раздел на FunPay
3. Придумает 12 тем через ИИ (плюс 3 обязательных товара)
4. На каждую тему напишет гайд, переведёт описание на английский
5. Загрузит гайд в твою папку на Google Drive
6. Выставит лот за 1 ₽ с авто-выдачей ссылки
7. Если игра есть в `data/wemod_games_list.txt` — выставит ещё лот за 60 ₽
8. Уберёт игру из списка и перейдёт к следующей

Остановить: `Ctrl + C`.

---

## Если что-то не так

| Что видишь | Что делать |
|---|---|
| `AI_WORKER_URL не задан` | Заполни пункт 4 и впиши адрес в `.env` |
| `Worker отклонил токен` | `AI_WORKER_TOKEN` в `.env` должен точно совпадать с секретом `PROXY_TOKEN` в Worker'е |
| `429 ai_limit_reached` | Закончились бесплатные 10 000 нейронов на сутки (сброс в 03:00 МСК). Бот сам подождёт |
| `не найден client_secrets.json` | Ключ Google не лежит рядом с `main.py` или назван иначе |
| `папка ... не найдена — поделись ею с сервисным аккаунтом` | Расшарь папку на email из `client_secrets.json` (пункт 3) |
| `FunPay недоступен` | Проверь funpay.com в браузере; если не открывается — впиши `PROXY_URL=...` в `.env` |
| `*.workers.dev` не открывается в браузере | Провайдер режет Cloudflare: проверь через мобильный интернет, при необходимости нужен VPS/прокси |
| `Библиотеки Google не установлены` | Не выполнен пункт 1: `py -m pip install -r requirements.txt` |
