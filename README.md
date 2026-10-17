# TEPPA Bot

Telegram-бот TEPPA на Python (aiogram 3): бронирование мест, удержание 12 минут,
оплата (Payme, Click, перевод на карту) и билет с QR-кодом. Работает на **тех же
данных, что и сайт**: Supabase-проект `baland-trip`, таблицы `trips`, `bookings`,
`payments`.

Бот — отдельный процесс рядом с сайтом. Он:

* показывает опубликованные рейсы, которые ещё не ушли (как сетка на сайте);
* ведёт форму бронирования: место на схеме автобуса → пассажир → телефон →
  email (необязательно, связывает бронь с кабинетом сайта) → обед;
* считает итог по правилам сайта: `цена рейса + 990 (сервисный сбор) + 100 000
  (обед)` — см. `app/lib/pricing.ts`;
* держит место **12 минут** и при истечении реально отменяет бронь
  (`bookings.status = 'Отменена'` → место снова свободно, уникальный индекс
  `bookings_active_seat_uniq`);
* принимает оплату:
  * **Payme / Click** — пока тоже чеком: покупатель переводит сумму и присылает
    скриншот чека (платёжный API подключим позже — код сборки ссылок уже есть
    в `payments/payme.py` и `payments/click.py`);
  * **перевод на карту** — покупатель присылает чек, проверка автоматическая,
    администратор подтверждает второй раз кнопкой в группе;
* **машина проверяет чек** (`bot/receipt_check.py`): фото не пустое и не
  дубликат, а если есть tesseract — ещё и сумма (== цене брони), дата
  (сегодня/вчера) и слова об оплате. Сомнительный чек авто-приняться не даёт —
  ждёт админа;
* пишет строку в `payments` (PK `booking_id`, `provider`, `amount`, `status`) —
  ровно ту же таблицу, которую использует сайт;
* выдаёт билет: текст, QR-код и **PDF-файл** (`bot/ticket_pdf.py`, тот же
  дизайн, что «Скачать билет в PDF» на сайте); токен в формате
  `<bookingId>.<32hex>` — QR взаимозаменяем с сайтом.

## Структура

```
teppa-bot/
├── main.py                 # запуск: polling (локально) или вебхук (VPS/Render) + uvicorn
├── Dockerfile              # образ для VPS (python:3.12-slim, непривилегированный юзер)
├── docker-compose.yml      # бот + Caddy (HTTPS) — деплой на VPS одной командой
├── deploy/Caddyfile        # reverse proxy: домен → bot:8080, сертификат Let's Encrypt
├── scripts/deploy.sh       # обновление на VPS: git pull + docker compose up -d --build
├── DEPLOY.md               # полная инструкция деплоя на VPS (Datapasa)
├── render.yaml             # Blueprint для Render.com — альтернативный деплой
├── .python-version         # 3.12 — версия Python локально, в образе и на Render
├── requirements.txt
├── .env.example            # все переменные с комментариями
├── bot/
│   ├── config.py           # чтение .env (секреты не логируются)
│   ├── db.py               # Supabase REST по service_role
│   ├── utils.py            # суммы, даты, QR-токен, дедлайн удержания
│   ├── texts.py            # все тексты интерфейса
│   ├── keyboards.py        # клавиатуры (меню, схема мест, оплата, админка)
│   ├── notify.py           # билет с QR + PDF, уведомления админам
│   ├── receipt_check.py    # машинная проверка чека: фото, дубликат, сумма, дата
│   ├── ticket_pdf.py       # билет в PDF (дизайн сайта), шрифты assets/fonts/
│   ├── runtime.py          # общее состояние процесса
│   ├── payments/
│   │   ├── payme.py        # Merchant API: 6 методов, Basic-авторизация, тийины
│   │   ├── click.py        # SHOP API: Prepare/Complete, MD5-подпись
│   │   └── service.py      # удержание, отметка «оплачено», доставка билета
│   ├── handlers/           # start / trips / booking / payment / admin
│   └── server.py           # FastAPI: /health, /telegram, /payme, /click webhook
└── tests/
    ├── test_signatures.py  # подписи, суммы, токен QR, RPC-сценарий Payme
    ├── test_flows.py       # 17 шагов: /start → бронь → чек → билет с QR и PDF
    ├── test_receipts.py    # чек в группу, автопроверка, авто-принятие, /internal/*
    ├── test_receipt_check.py # анализ чека (suspect/ok/unknown) и PDF-билет
    └── test_webhook.py     # вебхук Telegram: секрет, приём апдейта, ответы
```

## Установка

```bash
cd teppa-bot
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # и заполните
```

### Переменные `.env`

| Переменная | Зачем |
|---|---|
| `BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `BOT_USERNAME` | username бота без `@` — подставляется в ссылку с сайта |
| `ADMIN_IDS` | Telegram ID админов через запятую (уведомления, кнопки «подтвердить чек») |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | тот же проект, что у сайта (`baland-trip`) |
| `SITE_URL` | адрес сайта — возвращение после оплаты (`/hold?b=…`) |
| `PAYME_MERCHANT_ID`, `PAYME_SECRET_KEY`, `PAYME_TEST` | касса Payme; `PAYME_TEST=true` → `test.paycom.uz` |
| `CLICK_SERVICE_ID`, `CLICK_MERCHANT_ID`, `CLICK_SECRET_KEY` | сервис Click |
| `CARD_NUMBER`, `CARD_HOLDER` | куда переводить при оплате картой |
| `ADMIN_GROUP_ID` | группа Telegram, куда бот кладёт скриншоты чеков (пусто = чек в ЛС админам) |
| `RECEIPT_CHECK_SECONDS` | через сколько секунд чек принимается сам (`120` = 2 минуты) |
| `INTERNAL_SECRET` | секрет сайт ↔ бот для `/internal/approve|reject` (на сайте — `NUXT_BOT_INTERNAL_SECRET`) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | письмо покупателю при отклонении чека (пусто = без письма) |
| `HOLD_MINUTES` | окно удержания (по умолчанию `12`, как на сайте) |
| `SERVICE_FEE`, `LUNCH_PRICE` | 990 и 100000 — должны совпадать с сайтом |
| `HOST`, `PORT` | адрес HTTP-сервера вебхуков |
| `TELEGRAM_WEBHOOK_URL` | пусто = polling; задано = апдейты приходят POST'ом на `<url>/telegram/webhook`. На VPS подставляется из `BOT_DOMAIN` в `docker-compose.yml`, на Render — из `RENDER_EXTERNAL_URL`; руками задавать не нужно |
| `TELEGRAM_WEBHOOK_SECRET` | секрет заголовка `X-Telegram-Bot-Api-Secret-Token`; если пусто — генерируется при старте (на VPS лучше задать: `openssl rand -hex 16`) |
| `BOT_DOMAIN` | домен VPS без `https://` (например `bot.example.uz`) — нужен Caddy и webhook-режиму в compose |

Провайдер считается «готовым», только если заполнены **все** его ключи —
иначе кнопка способа оплаты просто не показывается (та же логика, что на сайте).

## Запуск

Локально (polling, без вебхуков Telegram):

```bash
.venv/bin/python main.py
```

На VPS (webhook, HTTPS от Caddy):

```bash
docker compose up -d --build      # см. DEPLOY.md
docker compose logs -f bot
```

В одном процессе поднимаются:

* приём апдейтов Telegram: **long-polling** локально или **вебхук**, если задан
  `TELEGRAM_WEBHOOK_URL` (режим видно в `GET /health` → `mode`);
* HTTP-сервер вебхуков (`GET /health` — проверка живости).

### Как указать Payme и Click адреса вебхуков

Нужен публичный HTTPS-адрес (домен или `tunnel` вроде cloudflared):

| Провайдер | Где в кабинете | URL |
|---|---|---|
| Payme | Merchant → «Настройки» → «Ссылка на обработчик» | `https://<домен>/payme/webhook` |
| Click | Сервис → «URL Prepare/Complete» (Merchant API) | `https://<домен>/click/webhook` |

После регистрации проверьте: `curl https://<домен>/health` → `{"ok":true,…}`.

## Деплой на свой VPS (Datapasa) — основной способ

Свой сервер 1 vCPU / 1 GB RAM: бот в Docker, HTTPS через Caddy (сертификат
Let's Encrypt выпускается сам), режим **вебхук**. Подробно — **[DEPLOY.md](DEPLOY.md)**,
кратко:

```bash
# 1. DNS: A-запись bot.example.uz → IP VPS
# 2. На сервере:
curl -fsSL https://get.docker.com | sudo sh
git clone https://github.com/9rf3/teppa-bot.git /opt/teppa-bot
cd /opt/teppa-bot && cp .env.example .env && nano .env   # секреты + BOT_DOMAIN
docker compose up -d --build
# 3. Проверка:
curl https://bot.example.uz/health        # {"ok":true,"mode":"webhook",…}
# 4. Обновления:
./scripts/deploy.sh
```

Что даёт этот вариант:

* `BOT_DOMAIN` в `.env` → compose сам выставляет
  `TELEGRAM_WEBHOOK_URL=https://<домен>`, Caddy получает сертификат;
* ноль «спящих» минут в отличие от бесплатного Render — ответ мгновенный;
* публичные адреса платёжек: `https://<домен>/payme/webhook`,
  `https://<домен>/click/webhook`;
* `restart: unless-stopped` + автозапуск Docker — после перезагрузки сервера
  бот поднимается сам;
* логи ограничены (`10m × 3`), SSD 16 GB не забьётся.

## Деплой на Render.com

В репозитории лежит `render.yaml` — Blueprint собирает `pip install -r
requirements.txt` и запускает `python main.py`, версия Python берётся из
`.python-version`.

1. [render.com](https://dashboard.render.com) → **New → Blueprint** → подключить
   репозиторий `github.com/9rf3/teppa-bot` → **Apply**.
2. Дашборд → сервис → **Environment** → добавить секреты (в `render.yaml` их
   нет намеренно) и **Save**:

   | Переменная | Где взять |
   |---|---|
   | `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
   | `ADMIN_IDS` | Telegram ID админов через запятую |
   | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | тот же проект, что у сайта |
   | `PAYME_*`, `CLICK_*`, `CARD_*` | если есть; иначе пропустите — кнопка скроется |

   Затем **Manual Deploy → Clear build cache & deploy**.
3. Готово: `https://<имя-сервиса>.onrender.com/health` →
   `{"ok":true,"mode":"webhook",…}`.

Что настроено само:

* `PORT` (по умолчанию `10000`) и `RENDER_EXTERNAL_URL` подставляет Render; из
  второго бот вычисляет свой адрес и вызывает
  `setWebhook(https://<имя>.onrender.com/telegram/webhook)`.
* Эндпоинт защищён заголовком `X-Telegram-Bot-Api-Secret-Token`
  (`TELEGRAM_WEBHOOK_SECRET`; если не задан — генерируется при каждом старте,
  и так же заново уходит в `setWebhook`).
* `autoDeploy: true` — пуш в `main` сразу пересобирает сервис.

Мелочи, о которых надо знать:

* **Одновременно только один экземпляр.** Локальный `python main.py` при старте
  делает `deleteWebhook` — это выключит бота на Render (и наоборот: polling на
  Render невозможен, Telegram ответит 409). Перед деплоем остановите локального
  бота.
* **Бесплатный тариф** засыпает после 15 минут без входящих запросов и просыпается
  около минуты (Telegram повторит доставку — сообщение не теряется). Чтобы ответ
  был мгновенным всегда: тариф Starter или внешний cron, пингующий `GET /health`
  раз в 10 минут.
* После деплоя станут публичными адреса для кабинетов платёжек:
  `https://<имя>.onrender.com/payme/webhook` и `.../click/webhook`.
* Локально webhook-режим проверяется без Telegram:
  `TELEGRAM_WEBHOOK_URL=http://localhost:8080 TELEGRAM_WEBHOOK_SECRET=xxx .venv/bin/python main.py`
  (адрес без https — `setWebhook` пропускается, но `/telegram/webhook` работает).

## Оплата

1. **Payme / Click / карта (перевод)** — пока единый сценарий: покупатель
   переводит сумму и присылает скриншот чека; бот:

   * отвечает сразу «чек получен, проверка займёт около 2 минут»;
   * **сам проверяет чек** (`bot/receipt_check.py`): скачивает фото, смотрит,
     что оно настоящее (не белый/чёрный экран, не повтор уже присланного файла),
     а с OCR (tesseract в образе) сверяет сумму с ценой брони и дату чека;
   * пересылает фото в **группу админов** (`ADMIN_GROUP_ID`) с кнопками
     «✅ Подтвердить / ❌ Отклонить», строкой автопроверки и сохраняет в
     `payments.error_note` ссылку на этот пост (`https://t.me/c/…/…`) — она
     видна в админке сайта. Админы проверяют чек второй раз, параллельно с
     таймером;
   * через 2 минуты (`RECEIPT_CHECK_SECONDS`), если никто не отклонил **и
     автопроверка не нашла проблем**, чек принимается сам: бронь → `Оплачена`,
     покупателю — билет (текст + QR + PDF), пост в группе помечается
     «✅ Оплачено автоматически»;
   * если автопроверка что-то не понравилось — авто-принятия нет, админам
     уходит «⚠️ не прошёл автопроверку», решение за человеком.

2. **Payme (Merchant API)** — код готов и протестирован, но кнопка пока идёт
   по сценарию чека (п. 1); при подключении вернётся QR + ссылка
   `https://checkout.paycom.uz/<base64>`, где параметры
   `m=<merchant>;ac.booking_id=<id>;a=<тийины>;c=<return>;l=ru`, и вебхук
   `CheckPerformTransaction → CreateTransaction → PerformTransaction`
   (сумма в тийинах, 1 сум = 100 тийинов, ответ всегда HTTP 200).

3. **Click (SHOP API)** — тоже готов: ссылка `my.click.uz/services/pay?…`,
   `Prepare (action=0)` и `Complete (action=1)` с MD5-подписью полей
   в фиксированном порядке.

Любой успешный путь делает одно и то же: `payments.status = 'paid'`,
`bookings.status = 'Оплачена'`, `qr_token` выдаётся (если ещё нет), покупателю
приходит билет (текст, QR, PDF), админам — уведомление.

**Отклонение** (кнопкой в группе/ЛС или из админки сайта → `/internal/reject`):
бронь → `Отменена` (место снова в продаже), платёж → `cancelled`, покупателю —
короткий отказ в Telegram и письмо по SMTP (если настроен), пост с чеком в
группе помечается «❌ Чек отклонён». QR перестаёт действовать: проверка
билета на сайте смотрит на статус брони.

### Глубокая ссылка с сайта

Кнопка «Telegram-бот» на странице удержания открывает:

```
https://t.me/<BOT_USERNAME>?start=pay_<bookingId>
```

Бот в этом случае сразу показывает меню оплаты по брони (и возобновляет
таймер удержания — дедлайн считается от `bookings.created_at`, поэтому окно не
сдвигается).

## Админка

* `/admin` — панель: неподтверждённые оплаты, статистика за сегодня.
* Новая бронь → админам сообщение с суммой и телефоном.
* Чек по переводу → фото в группу `ADMIN_GROUP_ID` (или в ЛС) с кнопками
  подтверждения; ссылка на пост попадает в админку сайта (раздел «Платежи»).
* Время вышло → сообщение о снятой брони.

### Внутренние маршруты для админки сайта

| Маршрут | Что делает |
| --- | --- |
| `POST /internal/approve` `{booking_id}` | подтверждает чек: оплата + билет покупателю |
| `POST /internal/reject` `{booking_id, reason}` | отклоняет чек: бронь снята, место свободно, отказ в Telegram и письмом |

Оба требуют заголовок `X-Internal-Secret` = `INTERNAL_SECRET`. Без
`INTERNAL_SECRET` отвечают `503`, с чужим секретом — `403`. Дёргает сайт
(`baland-trip`, раздел «Платежи» в админке).

## Тесты

```bash
.venv/bin/python tests/test_signatures.py     # подписи Payme/Click, суммы, QR-токен
.venv/bin/python tests/test_flows.py          # 17 шагов: /start → бронь → чек → билет с QR и PDF
.venv/bin/python tests/test_receipts.py       # чек в группу, автопроверка, авто-принятие, /internal/*
.venv/bin/python tests/test_receipt_check.py  # анализ чека (suspect/ok/unknown), PDF-билет
.venv/bin/python tests/test_webhook.py        # вебхук Telegram: секрет, приём апдейта
# или: .venv/bin/pytest tests/
```

`test_flows.py` гоняет настоящие апдейты через Dispatcher: сессия Telegram и база —
заглушки, поэтому тест ловит ошибки вроде `command.payload` (в aiogram 3 есть только
`command.args`), из-за которых бот молчит. Проверяется без сети и без базы.

## Совместимость с сайтом

| | Значение |
|---|---|
| `bookings.price` | итог: рейс + 990 + (обед 100000) |
| `bookings.status` | `Новая → Оплачена` / `Отменена` |
| `bookings.qr_token` | `<bookingId>.<32hex>` (безопасность QR на сайте) |
| `payments` | одна строка на бронь, `provider ∈ payme\|click\|card`, `amount` в сумах |
| удалённые рейсы | `date + time` в прошлом не показываются |
| «Мои брони» | поиск по телефону (на сайте — по email; в форме бота email тоже спрашивается) |
# teppa-bot
