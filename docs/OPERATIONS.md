# Эксплуатация РИТМ

## Windows 11 + Docker Desktop (WSL 2)

1. Установите Docker Desktop (WSL 2 backend) и Git for Windows. Включите в Docker Desktop «Start Docker Desktop when you sign in».
2. Клонируйте репозиторий и создайте `.env` из `.env.example` (см. README). Пароли — только латиница и цифры: они подставляются в URL подключения.
3. `docker compose up -d --build`. Проверка: `docker compose ps` (`db` и `bot` — Up, `migrate` — Exited (0)).
4. Питание и сон: отключите сон ПК. Контейнеры перезапускаются сами (`restart: unless-stopped`), но только когда запущен Docker Desktop, а он стартует после входа пользователя в Windows. Если нужен запуск без входа, это отдельная настройка (автовход или служба), её нужно сделать осознанно.
5. Обновление: `git pull`, затем `docker compose up -d --build`. Миграции применяются автоматически сервисом `migrate`.
6. Режим webhook нужен только при публичном HTTPS-адресе. Для домашнего сервера за Tailscale используйте `BOT_MODE=polling`.

На хосте не требуется ничего Unix-специфичного: сборка и миграции идут внутри контейнеров. `.gitattributes` сохраняет LF в `*.sh`, поэтому скрипт инициализации БД работает и при клонировании на Windows.

## Резервные копии

### Создание

- Windows: `powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Dir D:\ritm-backups -KeepDays 14`
- Linux/macOS/WSL: `scripts/backup.sh backups 14` (или `make backup`)

Как работает:
- делается `pg_dump -Fc` внутри контейнера `db`;
- файл копируется командой `docker compose cp`, без перенаправлений PowerShell, которые портят бинарные данные;
- дампы старше `KeepDays` удаляются.

Хранилище по умолчанию — папка `backups\` в каталоге проекта (она в `.gitignore`). Рекомендуется отдельный диск или папка на нём.

Автоматизация на Windows — через Планировщик заданий:
- программа: `powershell.exe`;
- аргументы: `-ExecutionPolicy Bypass -File C:\ritm\scripts\backup.ps1 -Dir D:\ritm-backups`;
- расписание: ежедневно, например в 03:30.

### Проверка восстановления (без риска для живой базы)

- Windows: `powershell -ExecutionPolicy Bypass -File scripts\restore-check.ps1 -File D:\ritm-backups\ritm-YYYYMMDD-HHMMSS.dump`
- Linux/macOS/WSL: `make restore-check FILE=backups/ritm-....dump`

Скрипт:
- восстанавливает дамп во временную базу `ritm_restore_check`;
- сравнивает число строк ключевых таблиц с живой базой;
- проверяет, что восстановлены все RLS-политики (≥ 18);
- удаляет временную базу.

### Полное восстановление (разрушительно, выполнять вручную)

```powershell
docker compose stop bot
docker compose cp D:\ritm-backups\ritm-....dump db:/tmp/restore.dump
docker compose exec -T db dropdb -U postgres fitcoach
docker compose exec -T db createdb -U postgres -O fitcoach_owner fitcoach
docker compose exec -T db pg_restore -U postgres -d fitcoach --exit-on-error /tmp/restore.dump
docker compose start bot
```

На новом сервере сначала запустите `docker compose up -d db`: скрипт инициализации создаст роли. После этого восстанавливайте дамп.

### Ограничения

- Проверено: `backup.sh` и `restore-check.sh` на Docker Compose в Linux. Скрипты `.ps1` повторяют ту же логику, но в PowerShell не запускались (в среде разработки нет PowerShell). Первый прогон на сервере обязателен.
- Дампы не шифруются скриптом. Храните их на зашифрованном диске (BitLocker) и не выкладывайте в общий доступ: в них личные данные.
- Удалённый аккаунт исчезает из новых дампов сразу, а из старых — по мере их удаления (`KeepDays`). После восстановления старого дампа удалённые пользователи вернутся: повторите удаление.
- Бэкап — это копия на том же сервере. Для защиты от потери диска копируйте файлы на другой носитель.

## Данные пользователей

- **Экспорт**: ⚙️ Настройки → «📦 Экспорт данных». Бот присылает JSON только с записями этого пользователя.
- **Удаление**: ⚙️ Настройки → «🗑 Удалить аккаунт» → слово `УДАЛИТЬ`. Каскадно удаляются все личные таблицы; ожидающие черновики и журнал доставки напоминаний удаляются явно.
- Отдельные записи удаляются в «Мой день» → «✏️ Исправить»; шаблоны и программы архивируются без потери истории.
- У администратора нет функции просмотра чужих дневников. Runtime-роль БД ограничена RLS; владелец таблиц используется только миграциями.
