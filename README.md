# Telegram Bulk Exporter

**Export all your Telegram chats at once — and keep them in sync automatically.**

[![Tests](https://github.com/Yarusstyle-coder/telegram-bulk-exporter/actions/workflows/tests.yml/badge.svg)](https://github.com/Yarusstyle-coder/telegram-bulk-exporter/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows--first-0078D6.svg)](#install)

Local-first bulk exporter for Telegram chats, built on top of
[`tdl`](https://github.com/iyear/tdl) with a HTMX/Alpine/Tailwind web UI,
chunked export of huge chats, per-chat realtime auto-update, and SHA-256
dedup by hardlinks. The web UI is gated by a master password + TOTP 2FA;
**data on disk is not encrypted** — see [Security](#security).

> **Windows-first.** Developed and tested on Windows 10/11 — the `scripts\run.bat`
> launcher is the recommended way to run it. Linux/macOS and Docker paths are
> provided and should work, but they see far less testing today.

> English ↓ · Русский ниже.

## Why

Telegram Desktop's native export is painful in three ways:

- it only handles **one chat at a time**;
- there's **no incremental re-export** — you guess date ranges;
- media downloads are **single-stream**, and huge chats can time out or
  choke on a single export pass.

This tool wraps `tdl` in a FastAPI service with a local web UI. It lets you

- see every dialog in a checkboxed grid with avatars;
- pick media types, size caps, concurrency, and toggle "only new since last
  export";
- **export huge chats in resumable id-range chunks** — a crash or restart
  resumes from the last committed cursor instead of starting over;
- flip a per-chat **"Авто"** toggle to opt a chat into a background
  scheduler that keeps it in sync automatically as new messages arrive;
- watch live progress over a WebSocket;
- deduplicate identical media across chats by hardlinking them into a pool
  keyed by SHA-256 (the files look normal in Explorer — they just don't
  cost you disk twice);
- route Telegram traffic through a pool of SOCKS5 / MTProto proxies with
  auto-selection of the fastest live entry.

Telethon is used **only** for dialog metadata and profile-photo downloads;
all media and text export goes through `tdl`, which is ~10× faster for bulk.

## Security

**Model: the web UI is gated, the data is not.** A master password + TOTP
2FA protect access to the local web UI (`/login`, session cookie, rate
limiting). That is the entire scope of the protection.

- **Data on disk is stored unencrypted.** `state.db` is a plain SQLite
  database, and everything under `exports/` (`messages.json`,
  `messages.html`, downloaded media) is written as normal, readable files.
  Anyone with filesystem access — a stolen laptop, a leaked backup, another
  user on a shared machine — can read all of it directly, no password
  needed.
- If you need at-rest protection, use **OS-level full-disk encryption**
  (BitLocker on Windows, FileVault on macOS, LUKS on Linux). This tool does
  not attempt to replace that.
- `data/` holds your TOTP secret, backup-code hashes, and (optionally) the
  cached Telegram/tdl session. **Don't sync `data/` or `exports/` to a
  shared cloud drive without thinking about it** — that's the same as
  handing out your unencrypted chat history and, if `PERSIST_SESSIONS` is
  on, a way to resume your Telegram session elsewhere.
- Session cookie is httpOnly + SameSite=Strict; `Secure` is off because the
  app is localhost-only.

This is a deliberate trade-off, not an oversight: the goal is a fast,
dependency-light bulk exporter, not a vault. At-rest encryption may be
offered as an optional mode in a future release.

## Install

1. **Install `uv`** (Python package/venv manager this project uses):

   ```powershell
   # Windows
   winget install astral-sh.uv
   # or
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   ```bash
   # Linux / macOS
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Sync dependencies.** `dev` extras are only needed if you want to run
   the test suite:

   ```bash
   uv sync --extra dev   # for running tests too
   # or just:
   uv sync                # to run the app only
   ```

3. **Fetch the `tdl` binary.** It's AGPL-3.0 and is **not vendored** in
   this repo (`tools/tdl/` is gitignored); the script downloads the pinned
   release and verifies its checksum:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\fetch_tdl.ps1   # Windows
   ```
   ```bash
   bash scripts/fetch_tdl.sh                                         # Linux / macOS
   ```

4. **Copy the env template and fill in credentials.** Get
   `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from
   [my.telegram.org](https://my.telegram.org) → API development tools. You
   can also leave them empty here and enter them later through the UI on
   first run.

   ```bash
   cp .env.example .env
   ```

5. **Run it:**

   ```bat
   scripts\run.bat
   ```
   ```bash
   uv run python -m src.main
   ```

   Then open <http://127.0.0.1:8765>.

## First run

1. Set a **master password** (zxcvbn score ≥ 3 required) — this only
   protects the web UI, see [Security](#security).
2. Scan the **TOTP QR** with an authenticator app (Aegis / Raivo / Google
   Authenticator / 1Password) and save the 10 one-time **backup codes**.
3. **Log into Telegram** via Telethon: API id/hash (if not already in
   `.env`), phone number, SMS code, and 2FA password if you have one set.
4. **Authorize `tdl` separately.** Telethon and `tdl` have entirely
   separate sessions — logging into one does not log into the other. The
   dashboard shows an amber "Сессия tdl" card with a QR / code / desktop
   login flow. **Export will not work until this step is done.**

After that you land on `/chats`, tick the chats you want, hit **Export**,
and watch live progress on `/jobs`.

## Docker (optional)

```bash
cp .env.example .env   # required — the compose file reads it
docker compose up -d --build
```

`docker-compose.yml` maps `./data` → `/state` and `./exports` → `/exports`
as volumes so persisted data survives container recreation. Hardlink dedup
requires both to live on the same host filesystem.

## Data layout

```
./data/
  auth.json                  # TOTP secret + backup code hashes
  user_prefs.json            # UI prefs (auto-lock window choice, etc.)
  state.db                   # plain SQLite: chats, jobs, folders, chat state
  proxy_pool.json            # proxy pool + last-tested ms
  sessions/<n>.tgsess        # Telethon StringSession
  tdl/default/               # tdl's own bolt-DB peer cache + session
  avatars/<id>.jpg           # profile photos
  media_pool/<hash2>/<sha>.bin  # dedup pool — every unique file, once
./exports/
  chat_<slug>_<id>/          # messages.json / messages.html / media
```

None of the above is encrypted. Treat `data/` and `exports/` like any other
sensitive local folder.

## Chunked export of huge chats

Very large chats are exported in bounded id-range chunks instead of one
long-running `tdl` call. Each chunk's upper id is committed to
`ChatState.export_cursor_message_id` only after its media finishes
downloading, so a crash or restart resumes from the last completed chunk
rather than re-downloading from scratch or losing progress silently.

## Incremental exports & per-chat auto-update

`ChatState.last_exported_message_id` is the watermark for "only new since
last export": the exporter calls

    tdl chat export -c <chat> -i <last+1>,0 -T id -o ...

The cursor advances only **after** all media downloads succeed.

Flip the **"Авто"** toggle next to a chat in `/chats` to opt it into the
background auto-update scheduler. A long-running task periodically checks
every watched chat; if Telegram has messages newer than the last export,
it enqueues an incremental (`only_new=True`) sync job automatically — no
manual "Синхр." click needed. The scheduler shares the exact same
staleness rule, dedup, and job settings as a manual incremental export.

## Dedup

After each chat export the downloaded media directory is walked. Each file
is SHA-256-hashed; first sightings move the bytes into `media_pool` and
`os.link()` them back to their expected path; later sightings of the same
hash replace the new file with a hardlink and account `bytes_saved_via_links`.

On Windows hardlinks only work **within one NTFS volume**. If the export
directory lives on a different drive the code falls back to plain copying
with a warning in the log.

## Proxies

`PROXY=` (single URL) or `PROXIES=` (comma-separated) in `.env` seeds the
pool. The UI page **`/proxy`** lets you add/remove entries and re-test ping
without restarting. At startup, if `PROXY_AUTO_SELECT=true` (default), the
app does a TCP-handshake to every entry and picks the fastest one as
**active**.

Supported schemes:

```
mtproto://host:port?secret=HEX_OR_BASE64
mtproxy://...                              (alias of mtproto)
https://t.me/proxy?server=...&port=...&secret=...   (Telegram share link)
socks5://[user:pass@]host:port
socks5h://...                              (remote DNS via the proxy)
http://[user:pass@]host:port
```

### MTProto + tdl

`tdl` supports only SOCKS5 / SOCKS5H / HTTP. MTProto proxies work for
Telethon (chat list, avatars, login) but **not for tdl** which actually
moves the media. If your only path to Telegram is MTProto, run a local
bridge:

```bash
# https://github.com/9seconds/mtg
mtg run -b 127.0.0.1:1980 mtproto://host:port?secret=HEX_OR_BASE64
```

…then add `socks5://127.0.0.1:1980` to the pool. Telethon will keep using
the MTProto entry; the active SOCKS5 will be auto-selected for tdl.

### Dead proxies are not removed

Entries that fail / time out **stay in the pool** with a `last_status` marker.
When the network situation changes — e.g. you switch VPN region — the next
re-test (manual via `/proxy` → "Re-test all", or automatic every
`PROXY_TEST_INTERVAL_SECONDS`, default 30 min) will pick them up and switch
the active entry if one is now faster. To actually drop a proxy, hit the
trash icon in the UI.

## Private channels

After authenticating Telegram, every chat you're already a member of —
including private channels and supergroups — appears in `/chats`. Just hit
"Обновить список" once.

For a channel you're **not yet** in:

- Public: paste `@username` into the join field at the top of `/chats`.
- Private with invite link: paste `https://t.me/+ABCDEF…` (or the legacy
  `https://t.me/joinchat/ABCDEF…`). The app calls
  `ImportChatInviteRequest`, you'll see the chat after a refresh.

The exporter treats private channels exactly like any other chat — same
incremental sync, same dedup, same media-type filters.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `FLOOD_WAIT_X 42` in logs | tdl hit rate limit | Wait the advertised seconds; exporter auto-resumes after |
| `tdl binary not found at ...` | wrong `TDL_BINARY_PATH` | Point it at the absolute path of `tdl(.exe)` |
| export fails / "not authorized" | tdl needs its own login | Authorize tdl via the amber "Сессия tdl" card on the dashboard — Telethon being logged in is not enough |
| dedup reports `hardlink_failed_fallback_copy` | cross-volume or perms | Move `EXPORT_DIR` onto same volume as `DATA_DIR`, or run as admin |
| browser shows stale chat list | Telethon cache | Hit "Обновить список" — does a full `iter_dialogs` re-pull |

## Testing

```bash
uv sync --extra dev
uv run pytest tests/ -q       # full suite (392 tests)
uv run ruff check src/ tests/ # lint
uv run mypy src/              # types (non-strict)
```

## Roadmap

Planned, in rough priority order:

- **Optional at-rest encryption mode** — SQLCipher for the DB + sealed
  export containers, for setups where full-disk encryption isn't enough.
- **Voice/video transcription plugin** — Whisper-based transcripts rendered
  inline into the exported chat HTML.
- **Better Linux/macOS coverage** — the code paths exist; they need the same
  battle-testing the Windows path gets.

No dates promised — this is a spare-time project. Issues and PRs welcome.

---

# Telegram Bulk Exporter — RU

Локальный инструмент для массовой выгрузки чатов Telegram: обёртка вокруг
[`tdl`](https://github.com/iyear/tdl) с локальным web-интерфейсом,
чанкованным экспортом огромных чатов, realtime-докачкой по каждому чату и
дедупликацией медиа через hardlinks. Доступ к веб-UI защищён мастер-паролем
+ TOTP 2FA; **данные на диске не шифруются** — см. раздел «Безопасность».

> **Windows-first.** Проект разрабатывается и тестируется на Windows 10/11 —
> рекомендуемый способ запуска: `scripts\run.bat`. Пути для Linux/macOS и
> Docker есть и должны работать, но проверяются заметно реже.

## Зачем

Штатный экспорт Telegram Desktop:

- работает только **по одному чату**;
- **без инкремента** — приходится угадывать даты;
- медиа качает **в один поток**, а на огромных чатах экспорт может
  зависнуть или упасть по таймауту за один проход.

Этот инструмент решает всё это:

- все диалоги в одной таблице с чекбоксами и аватарками;
- выбираешь типы медиа, лимит размера, потоки, режим «только новые»;
- **экспорт огромных чатов чанками по диапазону id** — с резюме после
  падения или рестарта с последнего зафиксированного курсора, а не с нуля;
- переключатель **«Авто»** у чата включает фоновый планировщик, который
  сам докачивает новые сообщения по мере их появления;
- live-прогресс через WebSocket;
- дубликаты медиа между чатами становятся hardlink'ами в общий пул —
  Explorer видит файлы как обычные, но на диске они лежат один раз;
- трафик к Telegram можно пустить через пул SOCKS5 / MTProto прокси с
  авто-выбором самого быстрого живого узла.

Telethon используется **только** для списка диалогов и аватаров; медиа и
тексты качает `tdl` (он кратно быстрее).

## Безопасность

**Модель: защищён веб-UI, данные — нет.** Мастер-пароль + TOTP 2FA
защищают вход в локальный веб-интерфейс (`/login`, сессионная кука,
rate-limit). На этом защита заканчивается.

- **Данные на диске хранятся в открытом виде.** `state.db` — обычная
  SQLite-база, а всё в `exports/` (`messages.json`, `messages.html`,
  скачанные медиа) — обычные читаемые файлы. Любой с доступом к
  файловой системе — украденный ноутбук, утёкший бэкап, другой
  пользователь на общей машине — прочитает всё это напрямую, без пароля.
- Если нужна защита at-rest — используй **шифрование диска на уровне ОС**
  (BitLocker на Windows, FileVault на macOS, LUKS на Linux). Инструмент не
  пытается это заменить.
- В `data/` лежит твой TOTP-секрет, хеши backup-кодов и (опционально)
  закешированная сессия Telegram/tdl. **Не синкай `data/` и `exports/` в
  общий облачный диск не подумав** — это равносильно раздаче незашифрованной
  истории переписки, а при включённом `PERSIST_SESSIONS` — ещё и способ
  восстановить твою сессию Telegram в другом месте.
- Cookie сессии: httpOnly, SameSite=Strict. `Secure` выключен, т.к. сервис
  локальный.

Это осознанный компромисс, а не недосмотр: цель — быстрый и лёгкий по
зависимостям массовый экспортёр, а не хранилище-сейф. Шифрование at-rest
может появиться как опциональный режим в будущих версиях.

## Установка

1. **Установи `uv`** (менеджер пакетов/venv для Python, на котором держится проект):

   ```powershell
   # Windows
   winget install astral-sh.uv
   # или
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   ```bash
   # Linux / macOS
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Синхронизируй зависимости.** `dev`-экстра нужна только для прогона тестов:

   ```bash
   uv sync --extra dev   # если ещё и тесты гонять
   # или просто:
   uv sync                # только для запуска приложения
   ```

3. **Скачай бинарь `tdl`.** Он AGPL-3.0 и **не вкоммичен** в репозиторий
   (`tools/tdl/` в `.gitignore`); скрипт качает пиненный релиз и проверяет
   его checksum:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\fetch_tdl.ps1   # Windows
   ```
   ```bash
   bash scripts/fetch_tdl.sh                                         # Linux / macOS
   ```

4. **Скопируй шаблон `.env` и заполни креды.** `TELEGRAM_API_ID` /
   `TELEGRAM_API_HASH` берутся на [my.telegram.org](https://my.telegram.org)
   → API development tools. Можно оставить их пустыми здесь и ввести позже
   через UI при первом запуске.

   ```bash
   cp .env.example .env
   ```

5. **Запусти:**

   ```bat
   scripts\run.bat
   ```
   ```bash
   uv run python -m src.main
   ```

   Затем открой <http://127.0.0.1:8765>.

## Первый запуск

1. Задай **мастер-пароль** (нужен zxcvbn score ≥ 3) — он защищает только
   веб-UI, см. «Безопасность».
2. Отсканируй **TOTP QR** приложением-аутентификатором (Aegis / Raivo /
   Google Authenticator / 1Password) и сохрани 10 одноразовых
   **backup-кодов**.
3. **Залогинься в Telegram** через Telethon: API id/hash (если ещё не в
   `.env`), номер телефона, SMS-код и пароль 2FA, если он у тебя включён.
4. **Отдельно авторизуй `tdl`.** У Telethon и `tdl` полностью раздельные
   сессии — логин в одном не логинит второй. На дашборде — амбер-карточка
   «Сессия tdl» с флоу логина через QR / code / desktop.
   **Без этого шага экспорт не заработает.**

После этого ты попадаешь на `/chats`, отмечаешь нужные чаты, жмёшь
**Export** и смотришь живой прогресс на `/jobs`.

## Docker (опционально)

```bash
cp .env.example .env   # обязательно — compose-файл его читает
docker compose up -d --build
```

`docker-compose.yml` маппит `./data` → `/state` и `./exports` → `/exports`
как volumes, чтобы данные переживали пересоздание контейнера. Дедуп через
hardlink требует, чтобы оба тома лежали на одной файловой системе хоста.

## Где лежат данные

```
./data/
  auth.json                  # TOTP-секрет + хеши backup-кодов
  user_prefs.json            # UI-настройки (окно авто-лока и т.п.)
  state.db                   # обычная SQLite: chats, jobs, folders, chat state
  proxy_pool.json            # пул прокси + last-tested ms
  sessions/<n>.tgsess        # Telethon StringSession
  tdl/default/               # собственный кэш peer'ов bolt-DB tdl + сессия
  avatars/<id>.jpg           # фото профилей
  media_pool/<hash2>/<sha>.bin  # дедуп-пул — каждый уникальный файл один раз
./exports/
  chat_<slug>_<id>/          # messages.json / messages.html / медиа
```

Ничего из перечисленного не шифруется. Относись к `data/` и `exports/` как
к любой другой чувствительной локальной папке.

## Чанкованный экспорт огромных чатов

Очень большие чаты экспортируются ограниченными чанками по диапазону id,
а не одним долгим вызовом `tdl`. Верхняя граница id каждого чанка
фиксируется в `ChatState.export_cursor_message_id` только после того, как
медиа этого чанка полностью скачано — так что падение или рестарт
резюмируются с последнего завершённого чанка, а не перекачивают всё с нуля
и не теряют прогресс молча.

## Инкрементальный экспорт и авто-апдейт по чату

`ChatState.last_exported_message_id` — водяной знак для режима «только
новые с последнего экспорта»: экспортёр вызывает

    tdl chat export -c <chat> -i <last+1>,0 -T id -o ...

Курсор продвигается только **после** успешной докачки всех медиа.

Переключатель **«Авто»** рядом с чатом на `/chats` включает чат в фоновый
планировщик авто-апдейта. Долгоживущая задача периодически проверяет
каждый отслеживаемый чат; если в Telegram появились сообщения новее
последнего экспорта — планировщик сам ставит в очередь инкрементальную
(`only_new=True`) синхронизацию, без ручного клика «Синхр.». Планировщик
использует то же правило устаревания, дедуп и настройки задания, что и
ручной инкрементальный экспорт.

## Дедупликация

После каждого экспорта чата папка со скачанными медиа обходится. Каждый
файл хешируется по SHA-256; при первой встрече байты переносятся в
`media_pool`, а на их место кладётся `os.link()`; при повторной встрече
того же хеша новый файл заменяется хардлинком, а счётчик
`bytes_saved_via_links` растёт.

На Windows hardlinks работают только **в пределах одного NTFS-тома**. Если
папка экспорта на другом диске — код падает обратно на обычное
копирование с предупреждением в логе.

## Прокси

`PROXY=` (одна ссылка) или `PROXIES=` (через запятую) в `.env` засеивают
пул. Страница **`/proxy`** в UI позволяет добавлять/удалять записи и
перетестировать пинг без рестарта. При старте, если `PROXY_AUTO_SELECT=true`
(по умолчанию), приложение делает TCP-хендшейк к каждой записи и выбирает
самую быструю как **активную**.

Поддерживаемые схемы:

```
mtproto://host:port?secret=HEX_OR_BASE64
mtproxy://...                              (алиас mtproto)
https://t.me/proxy?server=...&port=...&secret=...   (ссылка Telegram share)
socks5://[user:pass@]host:port
socks5h://...                              (удалённый DNS через прокси)
http://[user:pass@]host:port
```

### MTProto + tdl

`tdl` поддерживает только SOCKS5 / SOCKS5H / HTTP. MTProto-прокси работают
для Telethon (список чатов, аватарки, логин), но **не для tdl**, который
реально качает медиа. Если у тебя есть только MTProto, подними локальный мост:

```bash
# https://github.com/9seconds/mtg
mtg run -b 127.0.0.1:1980 mtproto://host:port?secret=HEX_OR_BASE64
```

…и добавь `socks5://127.0.0.1:1980` в пул. Telethon продолжит использовать
MTProto-запись; для tdl автоматически выберется активный SOCKS5.

### Мёртвые прокси не удаляются

Записи, которые не отвечают/таймаутят, **остаются в пуле** с меткой
`last_status`. Когда сетевая ситуация меняется — например, ты сменил
регион VPN — следующий перетест (вручную через `/proxy` → «Re-test all»,
либо автоматически каждые `PROXY_TEST_INTERVAL_SECONDS`, по умолчанию 30
мин) подхватит их и переключит активную запись, если она теперь быстрее.
Чтобы реально удалить прокси — нажми на иконку корзины в UI.

## Приватные каналы

После авторизации в Telegram все чаты, в которых ты уже состоишь —
включая приватные каналы и супергруппы — появляются на `/chats`.
Достаточно один раз нажать «Обновить список».

Для канала, в котором ты ещё **не состоишь**:

- Публичный: вставь `@username` в поле присоединения вверху `/chats`.
- Приватный по инвайт-ссылке: вставь `https://t.me/+ABCDEF…` (или
  legacy `https://t.me/joinchat/ABCDEF…`). Приложение вызовет
  `ImportChatInviteRequest`, чат появится после обновления.

Экспортёр обрабатывает приватные каналы точно так же, как любой другой
чат — тот же инкрементальный синк, тот же дедуп, те же фильтры по медиа.

## Диагностика проблем

| Симптом | Причина | Решение |
|---|---|---|
| `FLOOD_WAIT_X 42` в логах | tdl упёрся в рейт-лимит | Подожди указанное число секунд; экспортёр сам резюмирует |
| `tdl binary not found at ...` | неверный `TDL_BINARY_PATH` | Укажи абсолютный путь до `tdl(.exe)` |
| экспорт падает / «not authorized» | tdl требует отдельного логина | Авторизуй tdl через амбер-карточку «Сессия tdl» на дашборде — авторизации Telethon недостаточно |
| дедуп пишет `hardlink_failed_fallback_copy` | другой том или права | Перенеси `EXPORT_DIR` на тот же том, что `DATA_DIR`, либо запусти от администратора |
| в браузере устаревший список чатов | кэш Telethon | Нажми «Обновить список» — делает полный re-pull `iter_dialogs` |

## Тестирование

```bash
uv sync --extra dev
uv run pytest tests/ -q       # полный набор (392 теста)
uv run ruff check src/ tests/ # линтер
uv run mypy src/              # типы (non-strict)
```

## Дорожная карта

В планах, в порядке приоритета:

- **Опциональный режим шифрования at-rest** — SQLCipher для БД + запечатанные
  контейнеры экспорта, для случаев, когда полнодискового шифрования мало.
- **Плагин транскрибации голосовых/видео** — Whisper-транскрипты прямо в
  HTML экспортированного чата.
- **Лучшее покрытие Linux/macOS** — код есть, нужна та же обкатка, что
  получает Windows-путь.

Без обещаний по срокам — проект развивается в свободное время. Issues и PR
приветствуются.

## Ограничения

- Не работает с Secret Chats (Telegram E2E не даёт к ним программного
  доступа).
- На Windows hardlinks — только в пределах одного NTFS-тома.
- Однопользовательский, локальный. Не ставь это на публичный сервер без
  дополнительного слоя защиты (обратный прокси, VPN, шифрование диска).

## Лицензия

MIT.
