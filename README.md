# CheckAnalyze

Сервис принимает кассовые чеки из Telegram и почтового ящика IMAP, сохраняет вложения на диск и позволяет подтверждать их через команду `/inbox` в боте. После подтверждения автоматически создаются записи в таблице `transactions` с привязкой к чеку.

## Возможности

- Чтение писем из IMAP и загрузка вложений PDF/JPG/PNG в файловое хранилище.
- Создание записей `email_inbox`, `receipts`, `receipt_items` и связь транзакций с чеками через `receipt_id`.
- Команда `/inbox` в Telegram с inline-кнопками: «Подтвердить общую сумму», «Разнести по позициям», «Отклонить».
- Очистка оригинальных файлов чеков после подтверждения или отклонения.
- Конфигурация через `.env`, systemd unit + timer для почтового сборщика и минимальные тесты на PostgreSQL.

## Развёртывание на Ubuntu 22.04

Ниже пример для сервера, где уже установлен PostgreSQL и создана база данных для проекта.

1. Установите системные зависимости и создайте пользователя/базу в PostgreSQL (если ещё не сделано):
   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-pip git postgresql postgresql-contrib
   sudo -u postgres createuser -P checkanalyze      # задайте пароль
   sudo -u postgres createdb -O checkanalyze checkanalyze
   ```

2. Клонируйте репозиторий и подготовьте виртуальное окружение:
   ```bash
   cd /opt
   sudo git clone https://example.com/checkanalyze.git CheckAnalyze
   cd CheckAnalyze
   sudo chown -R $USER:$USER .
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. Скопируйте шаблон `.env` и заполните значения:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Обязательно укажите реквизиты БД (`DB_*`), путь к каталогу чеков (`RECEIPTS_DIR`), параметры IMAP и токен Telegram-бота.

4. Создайте каталог для файлов чеков и выдайте права сервисному пользователю:
   ```bash
   mkdir -p /opt/CheckAnalyze/receipts
   chown $USER:$USER /opt/CheckAnalyze/receipts
   ```

5. Примените SQL-миграцию (можно запускать повторно — скрипт идемпотентный):
   ```bash
   source .venv/bin/activate
   psql "host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER" -f migrations/0001_prod_schema.sql
   psql "host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER" -f migrations/0001_prod_schema.sql
   ```

6. Установите и активируйте systemd unit и таймер для почтового сборщика:
   ```bash
   sudo cp deploy/checkbot-imap.service /etc/systemd/system/
   sudo cp deploy/checkbot-imap.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now checkbot-imap.timer
   ```
   Таймер запускает `checkanalyze.imap_fetcher` каждую минуту. Логи доступны через `journalctl -u checkbot-imap.service`.

7. Запустите Telegram-бота (через CLI или отдельный unit):
   ```bash
   source .venv/bin/activate
   python -m checkanalyze.cli telegram
   ```
   Для постоянной работы оформите systemd-сервис по аналогии с IMAP-таской.

## Переменные окружения

| Переменная         | Назначение |
|--------------------|-----------|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Доступ к PostgreSQL. |
| `RECEIPTS_DIR`     | Каталог для сохранения оригинальных файлов чеков. |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USE_SSL`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `IMAP_MAILBOX` | Параметры IMAP-сервера. |
| `TELEGRAM_BOT_TOKEN` | Токен бота от `@BotFather`. |
| `TELEGRAM_ADMIN_ID` | (опционально) ID оператора, которому разрешена работа с ботом. |
| `DEFAULT_USER_ID`  | ID пользователя по умолчанию, если email отправителя не найден. |

## Почтовый ящик

Создайте отдельный почтовый ящик (например, `receipts@example.com`) и включите IMAP. В `.env` укажите параметры подключения. Письма с вложениями PDF/JPG/PNG будут загружены в `RECEIPTS_DIR`, добавлены в `email_inbox`, а для каждого вложения создаётся запись `receipts` со статусом `pending`.

Письма с одинаковым `Message-ID` не создают дубли и помечаются как обработанные. Пользователь определяется по email (если в таблице `users` есть колонка `email`) либо через `DEFAULT_USER_ID`/первую запись в таблице.

## Telegram-бот

Команда `/inbox` выводит список чеков текущего пользователя со статусом `pending` и inline-кнопками:

- **Подтвердить общую сумму** — создаёт одну транзакцию на сумму `receipts.total_amount`, заполняет `transactions.receipt_id` и `source`.
- **Разнести по позициям** — создаёт транзакции по строкам `receipt_items`. Если у позиции нет `category_id`, используется первая доступная категория.
- **Отклонить** — помечает чек как `failed`.

Загруженные через Telegram файлы сохраняются как `source="telegram"` и также отображаются в `/inbox`.

## Очистка файлов

После успешного подтверждения (`confirmed`) или отклонения (`failed`) сервис удаляет оригинальный файл из `RECEIPTS_DIR`. Для контроля объёма диска можно периодически проверять каталог — в нём остаются только нев обработанные (`pending`) чеки.

## Качество кода и тесты

- Установите `pre-commit` и активируйте хуки: `pre-commit install`.
- Локальная проверка: `pre-commit run --all-files`.
- Тесты: `pytest`. Они поднимают временную БД PostgreSQL (через `pytest-postgresql`) и проверяют, что миграция применима дважды, а почтовый парсер создаёт записи и игнорирует дубликаты.

> ⚠️ Для запуска тестов требуется доступность утилит PostgreSQL (`initdb`, `pg_ctl`). В CI подготовьте образ с установленным сервером или Docker-контейнер.

## Makefile и полезные команды

В репозитории есть `Makefile` с базовыми целями:

```bash
make install   # установка зависимостей в текущем окружении
make lint      # ruff + black + mypy
make test      # pytest
make bot       # запуск Telegram-бота
make imap      # единичный запуск IMAP-пуллера
```

## Очередность действий оператора

1. Отправьте чек в Telegram или переадресуйте на выделенную почту.
2. В `/inbox` проверьте позиции, при необходимости скорректируйте категории в БД.
3. Подтвердите чек (общая сумма или позиции). Транзакции создадутся автоматически, файл удалится.
4. При ошибке используйте кнопку «Отклонить» — файл будет очищен, а чек помечен как `failed`.

## Системные файлы

- `deploy/checkbot-imap.service` — oneshot-сервис, запускающий `python3 -m checkanalyze.imap_fetcher`.
- `deploy/checkbot-imap.timer` — таймер на каждую минуту (`OnBootSec=30s`, `OnUnitActiveSec=60s`).

Эти файлы копируются в `/etc/systemd/system/` и активируются командой `systemctl enable --now checkbot-imap.timer`.
