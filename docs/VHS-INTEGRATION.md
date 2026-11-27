# VHS: внутренний API и ключи

Единая схема обмена для модулей, работающих на разных машинах.

```text
IP-камеры → VHS-Streamer (видео, RTSP, архив)
                  │  X-VHS-Key
                  ▼
             VHS-Viewer (мозаика, аналитика, события)
                  │  X-VHS-Key
                  ▼
            VHS-Security (карта, тревоги, уведомления)
```

Viewer может принимать **несколько Streamer одновременно**. Каждый Streamer
подключается отдельной записью с собственным URL и ключом.

## 1. Ключи и заголовки

Основной заголовок:

```http
X-VHS-Key: <service-key>
X-VHS-Service: viewer | security | streamer
```

Для обратной совместимости принимаются:

- `X-API-Token` — Streamer;
- `X-Ingest-Token` — Viewer/Security.

Направление | Кто хранит ключ | Заголовок
--- | --- | ---
Viewer → Streamer | Viewer, поле «API-ключ Streamer» | `X-VHS-Key`
Security → Viewer | Security, «Ключ Viewer для Security» | `X-VHS-Key`
Security → Streamer | Security, «Ключ Streamer» | `X-VHS-Key`
Viewer → Security (события) | Viewer `.env` | `X-VHS-Key`

## 2. Streamer

В настройках Streamer:

**Настройки → Безопасность → API-токен**

Этот токен вводится в Viewer для каждого Streamer.

Эндпоинты:

```http
GET /api/integration/info
GET /api/integration/streams      # X-VHS-Key
```

Проверка:

```bash
curl -H "X-VHS-Key: <streamer-key>" \
  http://192.168.1.10:8100/api/integration/streams
```

Ответ содержит `webrtc_url` с временным токеном MediaMTX. Ключ Streamer
не публикуйте в интернет.

## 3. Viewer и несколько Streamer

`viewer_config.yaml`:

```yaml
servers:
  - name: Объект-1
    url: http://192.168.1.10:8100
    api_key: <streamer-key-1>
  - name: Объект-2
    url: http://192.168.1.11:8100
    api_key: <streamer-key-2>

integration:
  ingest_key: <viewer-key-for-security>
  service_name: viewer
```

Через UI: **Настройки → Серверы VHS-Streamer** — добавить URL и ключ,
нажать «Проверить», затем «Обновить список потоков с серверов».

Viewer агрегирует потоки всех серверов; при нескольких серверах имя
потока получает префикс имени сервера.

## 4. Viewer → Security

В Viewer:

**Настройки → Внутренний API и ключи → Ключ Viewer для Security**

Сгенерировать/скопировать ключ.

В Security:

**Настройки → Интеграция VHS**

- Viewer URL: `http://<viewer-host>:8200`
- Ключ Viewer для Security: тот же ключ
- Streamer URL и ключ Streamer — при необходимости

Эндпоинты Viewer:

```http
GET /api/integration/state     # камеры + события
GET /api/integration/events    # события
GET /api/integration/snapshot/<path>
```

Все требуют `X-VHS-Key` (или `X-Ingest-Token`).

## 5. Ротация ключей

1. Сгенерировать новый ключ в настройках принимающей стороны.
2. Обновить ключ во всех клиентах (Viewer servers / Security).
3. Сохранить настройки.
4. Проверить `/api/integration/info` и загрузку потоков.
5. Удалить старый ключ из `.env`/конфигурации.

## 6. Сеть

Порты по умолчанию:

```text
Streamer: 8100 web, 8554 RTSP, 8889 WebRTC, 9997 MediaMTX API
Viewer:   8200
Security: 8300
```

Между машинами достаточно:

- Viewer → Streamer: 8100, 8889;
- Security → Viewer: 8200;
- Security → Streamer: 8100.

Порты не публикуются в интернет. Удалённый доступ — WireGuard/VPN.
