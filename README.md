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
  * **Payme** — Merchant API (JSON-RPC), вебхук `/payme/webhook`;
  * **Click** — SHOP API, вебхук `/click/webhook`;
  * **перевод на карту** — покупатель присылает чек, администратор подтверждает
    кнопкой в боте;
* пишет строку в `payments` (PK `booking_id`, `provider`, `amount`, `status`) —
  ровно ту же таблицу, которую использует сайт;
* выдаёт билет с QR-кодом: токен в формате `<bookingId>.<32hex>`, QR кодирует
  сам токен — как `app/lib/ticket.ts`, поэтому QR с сайта и из бота взаимозаменяемы.

## Структура

```
teppa-bot/
├── main.py                 # запуск: polling (локально) или вебхук (Render) + uvicorn
├── render.yaml             # Blueprint для Render.com — деплой одной кнопкой
├── .python-version         # 3.12 — версия Python и локально, и на Render
├── requirements.txt
├── .env.example            # все переменные с комментариями
├── bot/
│   ├── config.py           # чтение .env (секреты не логируются)
│   ├── db.py               # Supabase REST по service_role
│   ├── utils.py            # суммы, даты, QR-токен, дедлайн удержания
│   ├── texts.py            # все тексты интерфейса
│   ├── keyboards.py        # клавиатуры (меню, схема мест, оплата, админка)
│   ├── notify.py           # билет с QR + уведомления админам
│   ├── runtime.py          # общее состояние процесса
│   ├── payments/
│   │   ├── payme.py        # Merchant API: 6 методов, Basic-авторизация, тийины
│   │   ├── click.py        # SHOP API: Prepare/Complete, MD5-подпись
│   │   └── service.py      # удержание, отметка «оплачено», доставка билета
│   ├── handlers/           # start / trips / booking / payment / admin
│   └── server.py           # FastAPI: /health, /telegram, /payme, /click webhook
└── tests/
    ├── test_signatures.py  # подписи, суммы, токен QR, RPC-сценарий Payme
    ├── test_flows.py       # 16 шагов: /start → бронь → чек → билет с QR
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
| `HOLD_MINUTES` | окно удержания (по умолчанию `12`, как на сайте) |
| `SERVICE_FEE`, `LUNCH_PRICE` | 990 и 100000 — должны совпадать с сайтом |
| `HOST`, `PORT` | адрес HTTP-сервера вебхуков |
| `TELEGRAM_WEBHOOK_URL` | пусто = polling; задано = апдейты приходят POST'ом на `<url>/telegram/webhook`. На Render подставляется автоматически из `RENDER_EXTERNAL_URL` |
| `TELEGRAM_WEBHOOK_SECRET` | секрет заголовка `X-Telegram-Bot-Api-Secret-Token`; если пусто — генерируется при старте |

Провайдер считается «готовым», только если заполнены **все** его ключи —
иначе кнопка способа оплаты просто не показывается (та же логика, что на сайте).

## Запуск

```bash
.venv/bin/python main.py
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

1. **Payme** — в боте отправляется QR + ссылка
   `https://checkout.paycom.uz/<base64>`, где параметры
   `m=<merchant>;ac.booking_id=<id>;a=<тийины>;c=<return>;l=ru`. Payme вызывает
   вебхук: `CheckPerformTransaction → CreateTransaction → PerformTransaction`.
   Сумма проверяется в тийинах (1 сум = 100 тийинов), ответ всегда HTTP 200.
2. **Click** — ссылка `my.click.uz/services/pay?…&transaction_param=<id>`,
   затем `Prepare (action=0)` и `Complete (action=1)` с MD5-подписью полей
   в фиксированном порядке.
3. **Картой (перевод)** — покупатель присылает скриншот чека, админу приходит
   фото с кнопками «✅ Подтвердить / ❌ Отклонить»; при подтверждении бронь
   становится `Оплачена` и уходит билет.

Любой успешный путь делает одно и то же: `payments.status = 'paid'`,
`bookings.status = 'Оплачена'`, `qr_token` выдаётся (если ещё нет), покупателю
приходит билет с QR, админам — уведомление.

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
* Чек по переводу → фото с кнопками подтверждения.
* Время вышло → сообщение о снятой брони.

## Тесты

```bash
.venv/bin/python tests/test_signatures.py     # подписи Payme/Click, суммы, QR-токен
.venv/bin/python tests/test_flows.py          # 16 шагов: /start → бронь → чек → билет с QR
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
