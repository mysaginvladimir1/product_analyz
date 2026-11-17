# Чат-бот производственного аналитика для Битрикс24

Интеграция регистрирует чат-бота, подключает его к существующим чатам и по расписанию отправляет в чат ответ сервера производственного аналитика. Расписания хранятся в SQLite и сохраняются после перезапуска процесса.

## Используемые методы Битрикс24

- `imbot.v2.Bot.register` — идемпотентная регистрация бота;
- `im.chat.user.add` — добавление зарегистрированного бота в существующий чат от имени владельца входящего вебхука;
- `imbot.v2.Chat.Message.send` — отправка сообщения от имени бота.

Используется актуальное API чат-ботов 2.0. Старые `imbot.register`, `imbot.message.add` и события `ONIMBOT*` намеренно не используются.

Официальная документация:

- https://apidocs.bitrix24.ru/api-reference/chat-bots/chat-bots-v2/imbot.v2/bots/bot-register.html
- https://apidocs.bitrix24.ru/api-reference/chats/chat-users/im-chat-user-add.html
- https://apidocs.bitrix24.ru/api-reference/chat-bots/chat-bots-v2/imbot.v2/messages/chat-message-send.html

## Настройка

1. В Битрикс24 создайте входящий вебхук от пользователя, который имеет право добавлять участников в нужные чаты. Выдайте вебхуку права `im` и `imbot`.
2. Скопируйте `.env.example` в `.env` и задайте реальные значения. URL входящего вебхука и оба токена являются секретами и не должны попадать в Git.
3. Запустите один HTTP-сервис:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
   ```

4. Зарегистрируйте бота. Все управляющие запросы используют существующий Bearer-токен `ANALYST_API_TOKEN`:

   ```powershell
   $headers = @{ Authorization = "Bearer $env:ANALYST_API_TOKEN" }
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/bitrix/setup -Headers $headers
   ```

Повторный вызов безопасен: `imbot.v2.Bot.register` возвращает ранее созданного бота с тем же кодом.

## Подключение к чату

Бота можно добавить в чат вручную через интерфейс Битрикс24 либо вызвать API проекта. Во втором случае нужен числовой ID чата, а владелец входящего вебхука должен состоять в чате и иметь право добавлять участников:

```powershell
$body = @{ chat_id = 123; hide_history = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/bitrix/chats/connect -Headers $headers -ContentType application/json -Body $body
```

Для дальнейшей отправки идентификатор этого группового диалога будет иметь вид `chat123`.

## Создание расписания

`run_at` должен содержать часовой пояс. Пример запуска 18 ноября 2026 года в 08:00 по Москве:

```powershell
$body = @{
    trigger_type = "datetime"
    dialog_id = "chat123"
    query = "Сформируй отчёт по простоям за предыдущую смену"
    run_at = "2026-11-18T08:00:00+03:00"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/bitrix/schedules -Headers $headers -ContentType application/json -Body $body
```

Текущая реализация поддерживает событие `datetime`. Поле `trigger_type` оставлено явным, чтобы позднее можно было добавить календарные и производственные события без изменения контракта.

Полезные эндпоинты:

- `GET /api/bitrix/status` — состояние конфигурации и планировщика;
- `POST /api/bitrix/dispatch` — немедленно проверить цепочку аналитик → бот → чат;
- `GET /api/bitrix/schedules` — список заданий;
- `GET /api/bitrix/schedules/{id}` — состояние конкретного задания;
- `DELETE /api/bitrix/schedules/{id}` — отменить ожидающее или неуспешное задание.

При временной ошибке запрос повторяется до `BITRIX24_MAX_ATTEMPTS` раз с паузой `BITRIX24_RETRY_DELAY`. После исчерпания попыток задание получает статус `failed`. При аварийном завершении во время отправки применяется доставка «как минимум один раз»: редкий повтор сообщения возможен, если Битрикс24 принял сообщение, но процесс не успел записать результат в SQLite.
