# CheckAnalyze

Сервис для автоматического разбора кассовых чеков в PDF и формирования команд для записи расходов/доходов. Источники чеков:

* Telegram-бот (принимает PDF-файлы как документы);
* почтовый ящик IMAP (принимает письма с PDF-вложениями).

После обработки чек разбирается на позиции, каждой позиции подбирается категория расходов и формируется строка формата `E/I сумма валюта точность комментарий`, готовая для вставки в систему учёта.

## Возможности

* Извлечение текста из PDF (pdfplumber) с fallback на OCR (pytesseract) для сканов.
* Определение продавца (название, ИНН) и общей суммы.
* Хранение данных по торговым точкам (`merchant_profiles`) и сохранение извлечённых позиций (`receipt_items`).
* Самообучение на основе подтверждённых пользователем категорий (`category_training_samples`) с помощью логистической регрессии и TF-IDF.
* Интеграция с PostgreSQL и расширение существующей схемы без нарушения текущего функционала.
* Интеграция с почтой: отображение email-адресов на пользователей (`email_identities`).

## Установка на VPS (Ubuntu 22.04)

1. Установите системные зависимости для обработки PDF и OCR:

   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-dev build-essential libpoppler-cpp-dev \
       tesseract-ocr tesseract-ocr-rus git
   ```

2. Клонируйте репозиторий и создайте виртуальное окружение:

   ```bash
   git clone https://github.com/<your-org>/checkanalyze.git
   cd checkanalyze
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -e .[dev]
   ```

3. Создайте файл `.env` рядом с `pyproject.toml` и заполните переменные окружения (см. ниже). Пример:

   ```bash
   cat > .env <<'EOF'
   DATABASE_URL=postgresql+psycopg2://checkanalyze:password@127.0.0.1:5432/checkanalyze
   TELEGRAM_BOT_TOKEN=123456789:abcdef
   TELEGRAM_ALLOWED_CHAT_ID=123456789
   IMAP_HOST=imap.example.com
   IMAP_USERNAME=receipts@example.com
   IMAP_PASSWORD=app-password
   IMAP_MAILBOX=INBOX
   DEFAULT_USER_ID=1
   EOF
   ```

4. Инициализируйте схему БД (на той же PostgreSQL, что и остальная система учёта):

   ```bash
   source .venv/bin/activate
   checkanalyze initdb
   ```

5. (Опционально) Настройте systemd-сервисы для бота и почтового воркера. Пример юнита:

   ```ini
   [Unit]
   Description=CheckAnalyze Telegram bot
   After=network.target

   [Service]
   WorkingDirectory=/opt/checkanalyze
   Environment="PYTHONPATH=/opt/checkanalyze"
   EnvironmentFile=/opt/checkanalyze/.env
   ExecStart=/opt/checkanalyze/.venv/bin/checkanalyze telegram
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

   Аналогично можно описать сервис `checkanalyze email-worker`.

Пакеты `libpoppler-cpp-dev` и `tesseract-ocr` необходимы для корректной работы `pdf2image` и `pytesseract`.

## Конфигурация

Переменные окружения (хранятся в `.env`):

| Переменная | Назначение |
|------------|------------|
| `DATABASE_URL` | Строка подключения SQLAlchemy к PostgreSQL (должна указывать на существующую БД). |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота, полученный у `@BotFather`. |
| `TELEGRAM_ALLOWED_CHAT_ID` | (опционально) ID чата/пользователя, которому разрешено пользоваться ботом. |
| `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD` | Параметры IMAP-доступа к почтовому ящику чеков. Рекомендуется выделить отдельный ящик. |
| `IMAP_MAILBOX` | Название IMAP-папки (по умолчанию `INBOX`). |
| `IMAP_CHECK_INTERVAL` | Интервал опроса почты в секундах (по умолчанию 120). |
| `IMAP_USE_SSL` | `1` для IMAPS (по умолчанию 1). |
| `DEFAULT_USER_ID` | Пользователь по умолчанию, если не найдено сопоставление email → пользователь. |

## Структура базы данных

К существующим таблицам добавлены:

* `merchant_profiles` — справочник торговых точек, хранит название, ИНН, категорию по умолчанию и подсказки.
* `receipt_items` — извлечённые позиции из чеков с прогнозами категорий.
* `category_training_samples` — обучающие примеры для модели классификации категорий.
* `email_identities` — связывает email-адреса с пользователями для почтового импорта.

`CategoryTrainingSample` и `ReceiptItem` используются для обучения и истории обработки. Таблица `receipts` позволяет хранить статус («pending»/«confirmed») и источник данных («telegram»/«email»).

## Настройка почтового ящика

1. Создайте отдельный email-адрес (например, `receipts@example.com`). Для публичных сервисов (Gmail, Яндекс и т.д.) включите IMAP и создайте отдельный пароль приложения.
2. Укажите IMAP-параметры в `.env` (`IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD`, при необходимости `IMAP_MAILBOX`).
3. Сопоставьте адреса отправителей с пользователями системы учёта:

   ```bash
   source .venv/bin/activate
   checkanalyze receipts link-email receipts@example.com --user-id 1
   checkanalyze receipts link-email another.user@example.com --user-id 2
   ```

   Если письмо пришло с нераспознанного адреса, будет использован `DEFAULT_USER_ID`.

## Работа с Telegram-ботом

1. Создайте бота через `@BotFather`, получите токен и добавьте его в `.env`.
2. (Опционально) Узнайте свой `chat_id` (например, через бота `@userinfobot`) и установите `TELEGRAM_ALLOWED_CHAT_ID`, чтобы ограничить доступ.
3. Запустите бота: `checkanalyze telegram`.
4. Основные команды в чате:

   * Отправьте PDF → бот создаст чек и покажет предложенные категории.
   * `/categories` — показать все доступные категории из вашей БД.
   * `/cat <номер строки> <категория>` — заменить категорию у строки (категорию можно указать по ID, коду или части названия). Для работы с прошлыми чеками используйте `/cat <id чека> <номер> <категория>`.
   * `/confirm` или `/confirm <id>` — подтвердить чек, зафиксировать обучение и получить итоговые команды `E ...`.
   * `/pending` — список неподтверждённых чеков.
   * `/receipt <id>` — показать конкретный чек повторно.

После подтверждения категории и команды сохраняются в базе и используются для самообучения модели; по мерчантам автоматически обновляется категория по умолчанию, если накопилось достаточно подтверждений.

## Работа через CLI

Для удалённых сценариев доступны вспомогательные команды:

```bash
checkanalyze receipts pending --user-id 1        # список неподтверждённых чеков
checkanalyze receipts show 12 --user-id 1        # подробности по чеку
checkanalyze receipts confirm 12 --user-id 1     # подтверждение и выдача команд
checkanalyze receipts categories --user-id 1     # список категорий
checkanalyze receipts link-email inbox@example.com --user-id 1
```

## Поток обработки и самообучение

1. Чек поступает через Telegram или email — создаётся запись в таблице `receipts`, а строки попадают в `receipt_items`.
2. Пользователь проверяет и при необходимости корректирует категории (`/cat`).
3. После команды `/confirm` строки переводятся в статус «confirmed», в `category_training_samples` добавляются примеры для обучения, модель классификации переобучается, а по мерчанту при необходимости обновляется категория по умолчанию.

Если вы вручную вносите транзакции в систему учёта, поле мерчанта остаётся необязательным — сервис работает и с пустым `merchant_id`.

## Запуск

Инициализируйте БД и запустите нужные сервисы через CLI:

```bash
source .venv/bin/activate
checkanalyze initdb                     # создание таблиц
checkanalyze telegram                   # запуск Telegram-бота (polling)
checkanalyze email-worker               # запуск почтового воркера (бесконечный цикл)
checkanalyze email-worker --no-forever  # одиночный проход без цикла
```

По умолчанию email-воркер ищет непрочитанные письма с PDF-вложениями, определяет пользователя по email-адресу (`email_identities`) либо использует `DEFAULT_USER_ID`, обрабатывает вложения и отмечает письмо как прочитанное.

## Обучение модели категорий

При подтверждении чека (`/confirm` или `checkanalyze receipts confirm ...`) все строки записываются в `category_training_samples`. Модель классификации переобучается автоматически, а также обновляется базовая категория у мерчанта, если большинство строк чека относится к одной статье.

## Тестирование

```bash
pytest
```

Тесты используют in-memory SQLite, переопределяя подключение через переменные окружения.

## Ограничения и дальнейшие улучшения

* Разбор PDF выполняется эвристически. Для сложных форматов может потребоваться тонкая настройка.
* Модель классификации обучается на локальных данных и может требовать регулярного пополнения обучающих примеров.
* Для работы OCR необходимы системные пакеты `tesseract-ocr` и языковые модели.
* Email-воркер не отправляет уведомления — обработанные чеки удобно просматривать через Telegram или CLI.

