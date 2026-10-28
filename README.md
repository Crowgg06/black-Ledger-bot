# Black Ledger — CRM-бот для Telegram

Бот для учёта продаж: каждый сотрудник пишет ему лично и добавляет продажи
командой `/add`, данные общие для всех (один файл базы данных), а владелец
видит статистику и может подключить групповой чат, куда бот будет дублировать
каждую новую продажу.

## 1. Получить токен бота (1 минута)

1. Откройте в Telegram **@BotFather**.
2. Отправьте `/newbot`, придумайте имя и username (должен заканчиваться на `bot`).
3. BotFather пришлёт токен вида `123456789:AAH...` — это и есть `BOT_TOKEN`.

## 2. Узнать свой Telegram ID

1. Откройте **@userinfobot** и отправьте ему любое сообщение.
2. Он пришлёт в ответ ваш числовой ID — это `OWNER_ID` (только у владельца
   будет доступ к настройкам курса, комиссии и сбросу данных; остальные
   сотрудники просто добавляют продажи).

## 3. Настроить

Скопируйте `.env.example` в `.env` и впишите туда значения:

```
BOT_TOKEN=123456789:AAH...
OWNER_ID=111222333
```

## 4. Запустить локально (проверить, что всё работает)

```bash
python -m venv venv
source venv/bin/activate        # на Windows: venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

Откройте бота в Telegram и отправьте `/start`. Попробуйте `/add`.

Пока вы держите `python bot.py` запущенным на своём компьютере — бот работает.
Как только закроете терминал или выключите компьютер — бот отключится, и
никто не сможет им пользоваться. Поэтому для постоянной работы нужно
разместить его на сервере (см. ниже).

## 5. Разместить бота, чтобы он работал круглосуточно

### Вариант А — бесплатно, Railway.app (проще всего)

1. Зарегистрируйтесь на [railway.app](https://railway.app) (можно через GitHub).
2. Залейте эту папку в свой репозиторий на GitHub (или воспользуйтесь
   Railway CLI, если не хотите заводить репозиторий — `railway init` и
   `railway up` из этой папки).
3. В Railway: **New Project → Deploy from GitHub repo**, выберите репозиторий.
4. В настройках проекта (Variables) добавьте `BOT_TOKEN` и `OWNER_ID` —
   те же значения, что в `.env`.
5. Railway сам увидит `Procfile` (`worker: python bot.py`) и запустит бота.
   Бесплатного стартового баланса Railway (обновляется ежемесячно) обычно
   достаточно для одного такого бота с небольшой нагрузкой.

⚠️ Важно: на Railway диск по умолчанию не сохраняется между перезапусками
контейнера. Чтобы файл `crm.db` не терялся при каждом деплое, подключите
**Volume** (Railway → ваш сервис → Settings → Volumes → добавить том,
примонтировать в `/app` или в папку, откуда запускается `bot.py`) и
укажите `DB_PATH=/data/crm.db`, где `/data` — путь внутри примонтированного
тома.

### Вариант Б — дешёвый VPS (надёжнее для долгой работы)

Любой VPS за $3–5/мес (Hetzner, DigitalOcean, Timeweb и т.п.) подойдёт:

```bash
# на сервере
sudo apt update && sudo apt install -y python3-venv git
git clone <ваш репозиторий>
cd telegram-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # и впишите токен и свой ID
```

Чтобы бот работал постоянно и перезапускался сам после сбоя/перезагрузки
сервера, создайте systemd-сервис `/etc/systemd/system/blackledger.service`:

```ini
[Unit]
Description=Black Ledger Telegram bot
After=network.target

[Service]
WorkingDirectory=/root/telegram-bot
ExecStart=/root/telegram-bot/venv/bin/python bot.py
EnvironmentFile=/root/telegram-bot/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl enable --now blackledger
sudo systemctl status blackledger   # проверить, что запустился
```

Здесь файл `crm.db` лежит прямо на сервере и никуда не денется — простой и
надёжный вариант для долгой работы.

## 6. Как этим пользоваться

- `/start` — регистрация, главное меню.
- `/add` — добавить продажу (бот сам спросит услугу, сумму, валюту, дату,
  клиента, аккаунт, партнёра, комиссию, расходы, заметку — на каждый шаг
  можно ответить «-», чтобы пропустить необязательное).
- `/stats` — выручка за сегодня / неделю / месяц / всего, средний чек, чистыми.
- `/sales` — последние продажи (с кнопкой «Ещё»).
- `/services` — разбивка выручки по услугам.
- `/clients` — список клиентов с суммами и датой последней покупки.
- `/accounts`, `/addaccount Имя`, `/delaccount Имя` — управление аккаунтами.
- `/partners`, `/addpartner Имя`, `/delpartner Имя` — управление партнёрами.
- `/export` — CSV со всеми продажами.
- Только владелец (`OWNER_ID`): `/setrate`, `/setcommission`, `/setcurrency`,
  `/setgroup` (выполнить в групповом чате — туда будут дублироваться все
  новые продажи), `/resetdata` + `/confirmreset`.

Добавьте сотрудников в бота — просто дайте им ссылку `t.me/ваш_бот` и
попросите нажать `/start`. Все видят и дополняют одну и ту же базу.
