# Деплой на VPS (Datapasa)

TEPPA bot на своём сервере: **Docker Compose** (бот) + **Caddy** (HTTPS,
сертификат Let's Encrypt выпускается и продлевается сам).

Тариф VPS: 1 vCPU / 1 GB RAM / 16 GB SSD / 1 TB трафика — хватает с запасом:
Python-процесс ≈ 70–100 МБ, Caddy ≈ 20–30 МБ, система ≈ 250 МБ.

```
Интернет ──► :80/:443 Caddy (HTTPS) ──► bot:8080 (внутренняя сеть Docker)
                                              │
                        Telegram ◄── /telegram/webhook
                        Payme    ◄── /payme/webhook
                        Click    ◄── /click/webhook
                        Сайт     ◄── /internal/approve|reject
```

## 1. DNS

В панели домена создайте **A-запись** на IP VPS, например:

| Тип | Имя | Значение |
|---|---|---|
| A | `bot` | `<IP VPS>` |

Итоговый адрес: `https://bot.example.uz` — именно он станет
`BOT_DOMAIN`. Проверьте: `ping bot.example.uz` отдаёт IP сервера.

## 2. Первый запуск сервера

SSH на VPS (логин/пароль или ключ — из панели Datapasa):

```bash
# Docker (ставит и docker compose в комплекте, включает автозапуск при загрузке)
curl -fsSL https://get.docker.com | sudo sh

# чтобы не нужен был sudo в docker-командах
sudo usermod -aG docker $USER
newgrp docker

# firewall: SSH + HTTP/HTTPS (панель Datapasa тоже может иметь свой firewall —
# разрешите 80/443 и там)
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable
```

## 3. Клонирование и настройка

```bash
sudo mkdir -p /opt && cd /opt
git clone https://github.com/9rf3/teppa-bot.git
cd teppa-bot

cp .env.example .env
nano .env        # заполнить секреты, см. таблицу ниже
```

### Что обязательно поменять в `.env`

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен [@BotFather](https://t.me/BotFather) |
| `BOT_USERNAME` | username бота без `@` |
| `ADMIN_IDS` | Telegram ID админов через запятую |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | проект `baland-trip` (Dashboard → Project Settings → API) |
| `SITE_URL` | адрес сайта (`https://teppa.vercel.app`) |
| **`BOT_DOMAIN`** | ваш домен, например `bot.example.uz` — **без** `https://` |
| `TELEGRAM_WEBHOOK_SECRET` | `openssl rand -hex 16` — фиксированный секрет вебхука |
| `PAYME_*`, `CLICK_*`, `CARD_*` | если платёжки подключаете; иначе кнопка способа оплаты скроется |
| `ADMIN_GROUP_ID` | группа для скриншотов чеков (инструкция в `.env.example`) |
| `INTERNAL_SECRET` | тот же секрет, что `NUXT_BOT_INTERNAL_SECRET` на сайте |

`TELEGRAM_WEBHOOK_URL` вручную задавать **не нужно**: docker-compose сам
подставит `https://$BOT_DOMAIN`.

## 4. Запуск

```bash
docker compose up -d --build
docker compose ps          # bot: healthy, caddy: Up
docker compose logs -f bot # логи бота
```

Проверка:

```bash
curl https://bot.example.uz/health
# {"ok":true,"mode":"webhook","payme":true,"click":true,"card":true}
```

В логах бота строка `Режим WEBHOOK: Telegram шлёт апдейты на https://bot.example.uz/telegram/webhook`
— значит, Telegram принял вебхук.

## 5. Вебхуки платёжных систем

Публичные адреса теперь всегда одинаковые:

| Провайдер | Где указать | URL |
|---|---|---|
| Payme | Merchant → «Настройки» → «Ссылка на обработчик» | `https://bot.example.uz/payme/webhook` |
| Click | Сервис → «URL Prepare/Complete» | `https://bot.example.uz/click/webhook` |

Сайт дёргает бота на `https://bot.example.uz/internal/approve|reject`
(заголовок `X-Internal-Secret` = `INTERNAL_SECRET`).

## 6. Обновления

```bash
cd /opt/teppa-bot
./scripts/deploy.sh        # git pull + docker compose up -d --build
```

Или вручную: `git pull && docker compose up -d --build`.

## Полезные команды

```bash
docker compose ps                    # состояние
docker compose logs -f bot           # логи бота
docker compose logs -f --tail=100 caddy  # кто и с каким кодом стучался
docker compose restart bot           # перезапуск бота
docker compose down                  # остановить всё
docker compose up -d --build         # поднять снова
docker system prune -f               # почистить старые образы (16 GB SSD!)
```

## Важно

* **Одновременно только один экземпляр.** Локальный `python main.py` при
  старте делает `deleteWebhook` и выключит бота на VPS (а на VPS он живёт на
  вебхуке — polling там даст 409). Перед запуском на сервере остановите
  локального бота. Наоборот, чтобы вернуться к локальному запуску:
  `docker compose down` → потом `python main.py` сам снимет вебхук.
* **SSD 16 GB** — за ними следят логи Docker (`max-size: 10m`, хранится 3
  файла на сервис). Раз в пару месяцев: `docker system prune -f`.
* **Перезагрузка сервера** — Docker включает себя сам, `restart:
  unless-stopped` поднимет бота и Caddy обратно. Дополнительные systemd-юниты
  не нужны.
* **Ключи и `.env` — единственные локальные данные.** База в Supabase,
  состояние вебхуков тоже там. Рекомендуем раз в месяц скачивать `.env`
  с сервера (`scp root@IP:/opt/teppa-bot/.env ./backup.env`).
* **Caddy сам продлевает сертификат** — ничего делать не надо; падение
  продления видно в `docker compose logs caddy`.
* **Отладка вебхуков:** `curl -H 'X-Internal-Secret: …' -X POST
  https://bot.example.uz/internal/approve -H 'Content-Type: application/json'
  -d '{"booking_id":"…"}'`.

## Если что-то не так

| Симптом | Что смотреть |
|---|---|
| `404` на `/health` | Caddy не поднялся: `docker compose logs caddy`, открыты ли 80/443 |
| `502 Bad Gateway` | бот упал: `docker compose logs bot`, `docker compose ps` |
| Certbot/Let's Encrypt ошибка | A-запись домена не на этот IP или порт 80 закрыт |
| Бот молчит | `docker compose logs bot` → нет строки `Режим WEBHOOK`; проверьте `BOT_DOMAIN` |
| Ошибка compose «Укажите BOT_DOMAIN» | в `/opt/teppa-bot/.env` нет строки `BOT_DOMAIN=…` |
| 409 от Telegram | на сервере и локально одновременно работают два экземпляра |
