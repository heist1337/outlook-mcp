# Outlook MCP

[![Check](https://github.com/vampire1337/outlook-mcp/actions/workflows/check.yml/badge.svg)](https://github.com/vampire1337/outlook-mcp/actions/workflows/check.yml)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
[![MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Почта и календарь **локального Exchange через EWS** для MCP-агентов.
[FastMCP](https://gofastmcp.com/) · Streamable HTTP · запуск через uv или Docker.

Поиск и чтение писем, вложения, папки, календарь, занятость и подписанные планы
изменений. Каждому пользователю нужны собственная учётная запись Exchange
и сетевой доступ к его серверу. Это коннектор EWS, а не Microsoft Graph.

## Запуск за несколько минут

Установить [uv](https://docs.astral.sh/uv/getting-started/installation/), затем:

```bash
git clone https://github.com/vampire1337/outlook-mcp.git
cd outlook-mcp
cp .env.example .env
uv run --locked python -c "import secrets; print(secrets.token_urlsafe(32))"
```

В `.env` указать `EWS_ENDPOINT`, `EWS_SERVER`, `EWS_EMAIL`, `EWS_USERNAME`,
`EWS_PASSWORD` и записать сгенерированный токен в `BRIDGE_API_TOKEN`.
При необходимости подключить корпоративный VPN. На Windows вместо `cp`
использовать `Copy-Item .env.example .env` в PowerShell.

```bash
uv run --locked --env-file .env outlook-mcp
```

Оставить терминал открытым. MCP доступен на `http://127.0.0.1:8080/mcp/`;
проверка процесса — `http://127.0.0.1:8080/healthz`.
Python и зависимости uv устанавливает по `uv.lock`.

## Подключение агента

В клиенте с HTTP MCP указать URL и заголовок:

```text
URL: http://127.0.0.1:8080/mcp/
Authorization: Bearer <ваш BRIDGE_API_TOKEN>
```

Для клиентов с `mcp.json` — [пример конфигурации](mcp.example.json).
Сначала вызвать `outlook_capabilities`, затем `mail_folders` или
`calendar_upcoming`. Полный [список инструментов и настроек](docs/usage.md).

| Нужно | Опция в `.env` |
| --- | --- |
| Текст писем | `EWS_MAIL_BODY_ENABLED=true` |
| Вложения | `EWS_MAIL_ATTACHMENTS_ENABLED=true` |
| Контекст календаря и подробности встреч | `EWS_CALENDAR_CONTEXT_ENABLED=true` |
| Названия встреч | `EWS_ALLOW_EVENT_SUBJECTS=true` |
| Контакты, задачи и прочие расширенные чтения | `EWS_EXTENDED_READS_ENABLED=true` |

## Docker

```bash
docker compose up --build -d
docker compose logs --tail=30
```

Нужен обычный Docker Engine с Compose. Состояние хранится в отдельном томе;
порт доступен только на локальном компьютере. [Детали](docs/usage.md#docker).

## Изменения и перенос

Запись выключена по умолчанию. Для неё нужны отдельный токен, явно разрешённые
группы операций и отдельное MCP-подключение. Используются
`outlook_prepare` → `outlook_execute` с журналом результатов;
при неизвестном результате сначала сверить состояние.
[Настройка записи](docs/usage.md#запись).

Коллегам передаётся ссылка на этот репозиторий. Каждый заполняет собственный
`.env`; пароли, токены, письма и рабочее состояние в репозиторий не входят.
При переносе существующей установки отдельно сохранить `.env` и `DATA_DIR`.

[Разработка](CONTRIBUTING.md) · [Безопасность](SECURITY.md) · [MIT](LICENSE)
