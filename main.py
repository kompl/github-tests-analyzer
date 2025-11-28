#!/usr/bin/env python3
import os
from pathlib import Path
from lib.report_service import ReportService

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
REPOS = ['hydra-server']  # <-- список репозиториев
# REPOS = ['hoper', 'hydra-server', "hydra-core", "hupo"]  # <-- список репозиториев
BRANCHES = ['v6.2']  # Анализируемая ветка
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 100  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать txt и HTML
SAVE_LOGS = False  # Оставлять .txt на диске?
FORCE_REFRESH_CACHE = False  # Принудительно игнорировать кэш и переизвлекать результаты

def analyse_repo(repo: str, branch: str):
    """Делегирует анализ ReportService."""
    service = ReportService(GITHUB_TOKEN, OWNER, workflow_file=WORKFLOW_FILE)
    service.analyze_repo(
        repo=repo,
        branch=branch,
        master_branch=MASTER_BRANCH,
        max_runs=MAX_RUNS,
        output_dir=OUTPUT_DIR,
        save_logs=SAVE_LOGS,
        force_refresh_default=FORCE_REFRESH_CACHE,
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

    for repo in REPOS:
        for branch in BRANCHES:
            try:
                analyse_repo(repo, branch)
            except Exception as e:
                print(f"🔥 Ошибка при обработке {repo}{branch}: {e}")
                raise e


if __name__ == '__main__':
    main()
