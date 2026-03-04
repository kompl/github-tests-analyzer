# filedecorator v2

Анализатор падений тестов в GitHub Actions CI. Собирает данные о запусках workflow, классифицирует поведение тестов (стабильно падающие, flaky, починенные) и генерирует HTML/JSON-отчёты.

Go-реализация с чистой фазовой архитектурой, параллельным сбором данных и конкурентной генерацией отчётов.

## Архитектура

Пайплайн состоит из 5 именованных фаз:

```
Parse   → Collect → Analyze → Enrich → Render
```

| Фаза | Описание |
|------|----------|
| **Parse** | Парсинг лог-файла Ruby hash формата в список репозиториев и веток |
| **Collect** | Сбор данных из GitHub API: скачивание логов/артефактов, парсинг результатов тестов, кэширование в MongoDB. Каждый запуск обрабатывается параллельно (worker pool) |
| **Analyze** | Классификация поведения тестов по матрице состояний: `stable_failing`, `fixed`, `flaky`, `single_failure`. Вычисление diff между запусками |
| **Enrich** | Обогащение данных: поиск момента начала стабильного падения (`stable_since`) через историю в MongoDB |
| **Render** | Генерация отчётов: HTML (по одному на репозиторий/ветку) и JSON (один общий). HTML и JSON строятся параллельно |

Каждая фаза имеет явный контракт данных (входные/выходные структуры). Фазы выполняются последовательно, но внутри Collect и Render используется параллелизм.

## Структура проекта

```
v2/
├── main.go                        # Точка входа, оркестрация фаз
├── config.yaml                    # Конфигурация
├── config/
│   └── config.go                  # Загрузка YAML-конфигурации
├── parse/
│   └── logparser.go               # Парсинг Ruby hash лога → map[repo][]branch
├── collect/
│   ├── collector.go               # Оркестрация сбора данных, worker pool
│   ├── github.go                  # Клиент GitHub API
│   ├── cache.go                   # MongoDB-кэш результатов парсинга
│   ├── extractor_logs.go          # Извлечение результатов из zip-логов
│   ├── extractor_artifacts.go     # Извлечение результатов из JUnit XML артефактов
│   └── models.go                  # CollectResult, RunMeta, вспомогательные структуры
├── analyze/
│   ├── analyzer.go                # Классификация поведения тестов, diff, статистика
│   └── models.go                  # AnalyzeResult, BehaviorAnalysis, RunDiff
├── enrich/
│   ├── enricher.go                # Поиск stable_since в MongoDB
│   └── models.go                  # EnrichResult
├── render/
│   ├── renderer.go                # Параллельная оркестрация HTML + JSON
│   ├── html.go                    # Построение данных для HTML-шаблона
│   ├── json.go                    # Генерация JSON-отчёта
│   └── report.html.tmpl           # Go html/template шаблон отчёта
└── internal/
    └── models.go                  # Общие типы: StringSet, TestDetail, RunMeta
```

## Требования

- Go 1.24+
- MongoDB 6.0+
- GitHub Personal Access Token с доступом к `actions` scope

## Установка и запуск

### 1. MongoDB

Запуск через docker-compose (из корня проекта):

```bash
docker compose up -d
```

MongoDB будет доступна на `localhost:27017` (логин: `root`, пароль: `example`).

### 2. Сборка

```bash
cd v2
go build -o filedecorator .
```

### 3. Конфигурация

Отредактировать `config.yaml` под нужные параметры (см. раздел ниже).

### 4. Запуск

```bash
export GITHUB_TOKEN="ghp_..."
./filedecorator -config config.yaml
```

Токен читается **только** из переменной окружения `GITHUB_TOKEN`. В конфигурационном файле токен не хранится.

## Конфигурация

```yaml
github:
  owner: "hydra-billing"        # Организация на GitHub
  workflow_file: "ci.yml"       # Имя файла workflow

mongo:
  uri: "mongodb://root:example@localhost:27017"
  db: "filedecorator_v2"        # База данных
  collection: "parsed_results"  # Коллекция для кэша

analysis:
  master_branch: "master"       # Эталонная ветка
  max_runs: 100                 # Максимум запусков для анализа
  ignore_tasks:                 # Задачи-исключения (не обрабатывать)
    - "ADM-3191"
    - "INT-570"

output:
  dir: "downloaded_logs"        # Директория для отчётов
  save_logs: false              # Сохранять скачанные логи на диск
  force_refresh_cache: false    # Игнорировать кэш, перезагружать данные
  generate_json: true           # Генерировать JSON-отчёт

phases:                         # Какие фазы выполнять
  - parse
  - collect
  - analyze
  - enrich
  - render

input:
  log_file: "1.log"                      # Входной лог-файл для фазы Parse
  repo_branches_file: "repo_branches.json" # Файл с репозиториями/ветками
```

Можно исключить отдельные фазы из списка `phases`. Например, если `repo_branches.json` уже существует, фазу `parse` можно убрать.

## Фазы

### Parse

**Вход:** файл `1.log` (лог из CI в формате Ruby hash)
**Выход:** `repo_branches.json` (JSON: `{repo: [branch, ...]}`

Парсит лог-файл регулярными выражениями, извлекает секции проектов и ключи версий. Преобразует версии в имена веток (`versionToBranch`). Фильтрует задачи из `ignore_tasks`.

### Collect

**Вход:** список репозиториев/веток + конфигурация
**Выход:** `CollectResult` (метаданные запусков, упавшие тесты, детали ошибок)

Работа фазы:
1. Запрос списка завершённых запусков workflow через GitHub API
2. Параллельная обработка каждого запуска (4 воркера):
   - Проверка кэша в MongoDB
   - Извлечение результатов из артефактов (JUnit XML)
   - Fallback: извлечение из zip-логов
   - Сохранение в MongoDB
3. Построение сводки: упавшие тесты по запускам, метаданные, общий пул деталей
4. Отдельно загружается последний запуск на master для сравнения

### Analyze

**Вход:** `CollectResult`
**Выход:** `AnalyzeResult` (классификация, diff между запусками, статистика)

Чистая вычислительная фаза без I/O. Строит матрицу состояний (pass/fail) для каждого теста по всем запускам и классифицирует поведение:

- **stable_failing** — падает стабильно от определённого запуска до последнего
- **fixed** — падал, но в последнем запуске прошёл
- **flaky** — нестабильный (больше 2 переходов pass/fail)
- **single_failure** — единичное падение

Также вычисляет diff между последовательными запусками (новые падения, починенные, уникальные).

### Enrich

**Вход:** `CollectResult` + `AnalyzeResult`
**Выход:** `EnrichResult` (дата начала стабильного падения для каждого теста)

Для каждого `stable_failing` теста ищет в MongoDB самый ранний запуск, в котором этот тест присутствует. Использует список ID запусков ветки из фазы Collect (без дополнительных вызовов GitHub API).

### Render

**Вход:** результаты всех фаз для каждого репозитория/ветки
**Выход:** HTML-файлы + JSON-файл

HTML-отчёты генерируются автономно для каждой пары репозиторий/ветка. JSON-отчёт объединяет данные по всем проектам. HTML и JSON строятся параллельно через `sync.WaitGroup`.

HTML-отчёт содержит:
- Секции поведения (стабильно падающие, починенные, flaky)
- Diff по каждому запуску (новые падения, починенные, уникальные, все)
- Древовидная группировка тестов по `::` разделителю
- Детали ошибок с возможностью раскрытия

JSON-отчёт содержит:
- Информацию о последнем запуске
- Статистику (количество уникальных падений, новых, flaky)
- Классификацию каждого упавшего теста с probable cause и fail rate

## MongoDB

БД: `filedecorator_v2`, коллекция: `parsed_results`.

Схема документа совместима с Python-версией (те же имена полей):

```json
{
  "schema": 2,
  "owner": "hydra-billing",
  "repo": "hydra-core",
  "run_id": 12345678,
  "created_at": "2026-03-02T12:00:00Z",
  "has_no_tests": false,
  "details_list": [
    {
      "test_name": "SomeClass::test_name | error message",
      "items": [
        {
          "file": "test-run.txt",
          "line_num": 0,
          "context": "Error details...",
          "project": "hydra-core-tests",
          "order_index": 0
        }
      ]
    }
  ]
}
```

Уникальный индекс: `(owner, repo, run_id)`.

Отличие от Python-версии — только имя БД (`filedecorator_v2` вместо `filedecorator`). Наименования полей идентичны.
