# Бэклог

Замечания `minor` из закрытых вех и находки «Should fix» аудитов. Формат: веха, источник, суть, предлагаемое исправление.

| Веха | Источник | Суть | Исправление |
| --- | --- | --- | --- |
| M0 | reports/critic/M0-cycle-3.json | Исключённая цитата заголовка вырезается из плана в любом месте: можно переместить её и добавить тело ключа с END-строкой | Вырезать цитату только в строке `\| AC-M0-04 \|` или ловить END-строку приватного ключа; один тест |
| M0 | reports/critic/M0-cycle-2.json | Сжатые файлы (gzip) не сканируются | Записано в known_limitations; общий сканер (например, gitleaks) — отдельной задачей |
| A | reports/audit/stage-A.md | `error.schema.json` не связывает `retryable` с `timeout_before_start` | if/then в схеме |
| A | reports/audit/stage-A.md | `conftest.py` не блокирует UDP `sendto` | Расширить блок сети |
| A | reports/audit/stage-A.md | Пример политики в спеке и `policies/approval/example.yaml` называют разные чаты | Выровнять |
| M1 | reports/critic/M1-cycle-3.json | `uv run mutmut run` в каталоге пакета падает без корневого `uv sync` | Документировать запуск из корня или добавить mutmut в dev-группу пакета |
| M1 | reports/critic/M1-cycle-3.json | Свой `check_coverage.py` (75 строк), хотя есть coverage-threshold (per-file branch min) | Заменить на coverage-threshold |
| M1 | reports/critic/M1-cycle-3.json | Файл состояния GR-13a переименовывается без fsync; после сбоя питания пустой файл блокирует старт | fsync файла и каталога; явная процедура восстановления |
| M1 | reports/critic/M1-cycle-3.json | OSError при записи состояния выходит из load() не как BundleError | Оборачивать в BundleError с причиной |
