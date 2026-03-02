#!/usr/bin/env python3
import json
import os
from pathlib import Path
from lib.report_service import ReportService
from lib.json_report import generate_json_report
from lib.log_parser import parse_log_to_repo_branches

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
IGNORE_TASKS = [
    'ADM-3191',
    'INT-570',
    'INT-653',
    'ADM-3173',
    'MIGR-421'
]
PHASES = [2]
# --- Фаза 1: Парсинг 1.log → сохранение в repo_branches.json ---
_repo_branches_path = Path(__file__).parent / 'repo_branches.json'
if 1 in PHASES:
    print("\n=== Фаза 1: Парсинг лога → repo_branches.json ===")
    _log_path = Path(__file__).parent / '1.log'
    if _log_path.exists():
        _parsed = parse_log_to_repo_branches(_log_path, ignore_tasks=IGNORE_TASKS)
        if _parsed:
            with open(_repo_branches_path, 'w', encoding='utf-8') as f:
                json.dump(_parsed, f, ensure_ascii=False, indent=2)
            print(f"✅ Собрано {len(_parsed)} проектов, сохранено в {_repo_branches_path.name}:")
            print(json.dumps(_parsed, ensure_ascii=False, indent=2))
        else:
            print("⚠ Не удалось извлечь данные из 1.log")
    else:
        print("⚠ Файл 1.log не найден")
    print("=== Фаза 1 завершена ===\n")

# --- Фаза 2 читает из repo_branches.json ---
REPO_BRANCHES = {}
try:
    with open(_repo_branches_path, encoding='utf-8') as f:
        _content = f.read().strip()
        REPO_BRANCHES = json.loads(_content) if _content else {}
except (FileNotFoundError, json.JSONDecodeError):
    REPO_BRANCHES = {}
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 100  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать txt и HTML
SAVE_LOGS = False  # Оставлять .txt на диске?
FORCE_REFRESH_CACHE = False  # Принудительно игнорировать кэш и переизвлекать результаты
GENERATE_JSON_REPORT = True  # Генерировать общий JSON-отчёт по всем проектам

def analyse_repo(repo: str, branch: str):
    """Делегирует анализ ReportService."""
    service = ReportService(GITHUB_TOKEN, OWNER, workflow_file=WORKFLOW_FILE)
    return service.analyze_repo(
        repo=repo,
        branch=branch,
        master_branch=MASTER_BRANCH,
        max_runs=MAX_RUNS,
        output_dir=OUTPUT_DIR,
        save_logs=SAVE_LOGS,
        force_refresh_default=FORCE_REFRESH_CACHE,
        generate_json=GENERATE_JSON_REPORT,
    )


def main():
    if not GITHUB_TOKEN:
        print("❌ Переменная окружения GITHUB_TOKEN не задана.")
        return
    OUTPUT_DIR.mkdir(exist_ok=True)

    if SAVE_LOGS:
        print(f"💾 Режим сохранения txt файлов включён. Папка: {OUTPUT_DIR}")
    else:
        print("🗑 Режим сохранения логов отключён (SAVE_LOGS = False)")
    # Сообщаем о режиме инвалидации кэша (по глобальному флагу или ENV)
    force_refresh_env = os.getenv('FORCE_REFRESH_CACHE', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if FORCE_REFRESH_CACHE or force_refresh_env:
        print("🧹 Инвалидация кэша включена (FORCE_REFRESH_CACHE)")

    if 2 not in PHASES:
        print("⏩ Фаза 2 пропущена")
        return

    print("\n=== Фаза 2: Анализ репозиториев ===")
    all_json_data = []
    for repo, branches in REPO_BRANCHES.items():
        for branch in branches:
            try:
                result = analyse_repo(repo, branch)
                if GENERATE_JSON_REPORT and result and result.get("json_data"):
                    all_json_data.append(result["json_data"])
            except Exception as e:
                print(f"🔥 Ошибка при обработке {repo}{branch}: {e}")
                raise e

    if GENERATE_JSON_REPORT and all_json_data:
        generate_json_report(all_json_data, OUTPUT_DIR)
    print("=== Фаза 2 завершена ===")


if __name__ == '__main__':
    main()
