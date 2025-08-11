#!/usr/bin/env python3
import os
from pathlib import Path
from lib.html import HtmlReportBuilder
from lib.analyze import GitHubWorkflowAnalyzer, TestAnalysisResults

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
REPOS = ['hupo']  # <-- список репозиториев
# REPOS = ['hoper', 'hydra-server', "hydra-core", "hupo"]  # <-- список репозиториев
BRANCH = 'fix_tests'  # Анализируемая ветка
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 3  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать txt и HTML
SAVE_LOGS = False  # Оставлять .txt на диске?

def analyse_repo(repo: str):
    """Анализирует репозиторий с использованием нового анализатора."""
    print(f"\n================= 📁 Репозиторий: {repo} =================")

    # Инициализируем анализатор и объект результатов (кэш артефактов внутри анализатора)
    analyzer = GitHubWorkflowAnalyzer(GITHUB_TOKEN, OWNER, workflow_file=WORKFLOW_FILE)
    results = TestAnalysisResults(repo, BRANCH)

    # Создаём директорию для текущей ветки и HTML builder так, чтобы отчёты разных веток не перезаписывались
    branch_output_dir = OUTPUT_DIR / BRANCH.replace('/',
                                                    '_')  # Заменяем '/' чтобы избежать вложенных директорий в имени файла
    branch_output_dir.mkdir(parents=True, exist_ok=True)
    html_builder = HtmlReportBuilder(branch_output_dir / f'failed_tests_{repo}.html', repo, BRANCH)

    # Создаём папку для логов конкретного репозитория
    logs_dir = OUTPUT_DIR / f'{repo}_logs'
    if SAVE_LOGS:
        logs_dir.mkdir(parents=True, exist_ok=True)
    # Настраиваем сохранение txt-логов на уровне анализатора
    analyzer.configure_cache(save_logs=SAVE_LOGS, log_save_dir=logs_dir)

    # --- Анализ последних запусков без JSON-кэша анализа --- #
    # 1) Список падающих тестов в master (для сравнений)
    master_failed = set()
    if BRANCH != MASTER_BRANCH:
        print(f"📦 Ищем последний завершённый run '{WORKFLOW_FILE}' в '{MASTER_BRANCH}'…")
        master_failed = analyzer.get_master_failed_tests(repo, MASTER_BRANCH)
        print(f"✅ В {MASTER_BRANCH} упало {len(master_failed)} тестов.")
    results.set_master_failed(master_failed)

    # 2) Анализируем последние запуски ветки
    summary, meta, all_test_details = analyzer.analyze_repo_runs(repo, BRANCH, MAX_RUNS)
    results.add_run_data(summary, meta, all_test_details)

    if not summary:
        print("❌ Завершённых запусков нет.")
        return

    # JSON-кэша анализа больше нет; отчёт строится по свежескачанным данным

    # Добавляем детали тестов в HTML builder
    html_builder.add_test_details(all_test_details)

    # --- 3. Анализируем поведение тестов --- #
    behavior_analysis = results.analyze_test_behavior()

    print(f"\n=== 🔍 Анализ поведения тестов в {len(summary)} запусках ===")

    # Стабильно падающие тесты
    stable_failing = behavior_analysis['stable_failing']
    print(f"\n🔴 Стабильно падающие тесты ({len(stable_failing)} шт.):")
    if stable_failing:
        for test_name, info in stable_failing.items():
            marker = "" if BRANCH == MASTER_BRANCH else \
                (" (также в master)" if test_name in results.master_failed else " (только в ветке)")
            print(f"    • {test_name} (с {info['first_fail_run']}-го запуска){marker}")
        html_builder.add_section("🔴 Стабильно падающие тесты",
                                 [
                                     f"{test}{' (также в master)' if test in results.master_failed else ' (только в ветке)' if BRANCH != MASTER_BRANCH else ''} (с {info['first_fail_run']}-го запуска)"
                                     for test, info in stable_failing.items()])
    else:
        print("    ✅ Нет стабильно падающих тестов")
        html_builder.add_section("🔴 Стабильно падающие тесты", ["✅ Нет стабильно падающих тестов"])

    # Починенные тесты
    fixed_tests = behavior_analysis['fixed_tests']
    print(f"\n✅ Починенные тесты ({len(fixed_tests)} шт.):")
    if fixed_tests:
        for test_name, info in fixed_tests.items():
            print(f"    • {test_name} (починено в {info['next_commit_info']['title']}: {info['next_pr_link']})")
        html_builder.add_section("✅ Починенные тесты",
                                 [f"{test} (починено в {info['next_commit_info']['title']}: <href>{info['next_pr_link']}</href>)"
                                  for test, info in fixed_tests.items()])
    else:
        print("    ❌ Нет починенных тестов")
        html_builder.add_section("✅ Починенные тесты", ["❌ Нет починенных тестов"])

    # Нестабильные (flaky) тесты
    flaky_tests = behavior_analysis['flaky_tests']
    print(f"\n🟡 Нестабильные (flaky) тесты ({len(flaky_tests)} шт.):")
    if flaky_tests:
        for test_name, info in flaky_tests.items():
            pattern = info['pattern']
            fail_rate = (info['fail_count'] / info['total_runs']) * 100
            print(f"    • {test_name} (паттерн: {pattern}, падает {fail_rate:.1f}% времени)")
        html_builder.add_section("🟡 Нестабильные (flaky) тесты",
                                 [
                                     f"{test} (паттерн: {info['pattern']}, падает {(info['fail_count'] / info['total_runs']) * 100:.1f}% времени)"
                                     for test, info in flaky_tests.items()])
    else:
        print("    ✅ Нет нестабильных тестов")
        html_builder.add_section("🟡 Нестабильные (flaky) тесты", ["✅ Нет нестабильных тестов"])

    # Дифф по запускам
    print("\n=== 📊 Изменения падений тестов по последним запускам ===")
    for diff in results.get_run_diffs():
        info = diff['meta']
        added = diff['added']
        removed = diff['removed']
        only_here = diff['only_here']

        failed_total = len(diff.get('current', set()))
        # Добавляем число падений в метаданные, чтобы использовать в HTML-отчёте
        info['failed'] = failed_total
        print(f"\n📦 {info['title']} | {info['ts']} | {info['concl']} | failed: {failed_total} | {info['link']}")

        # Начинаем новую секцию run'а в HTML
        html_builder.start_run_section(info)

        print(f"➕ Новые падения ({len(added)} шт.):" if added else "➕ Новые падения: нет")
        if added:
            for t in sorted(added):
                marker = "" if BRANCH == MASTER_BRANCH else \
                    (" (также в master)" if t in results.master_failed else " (только здесь)")
                print(f"    {t}{marker}")
        html_builder.add_run_section("➕ Новые падения",
                                     [
                                         f"{t}{'' if BRANCH == MASTER_BRANCH else ' (также в master)' if t in results.master_failed else ' (только здесь)'}"
                                         for t in added])

        print(f"✔ Починились ({len(removed)} шт.):" if removed else "✔ Починились: нет")
        if removed:
            for t in sorted(removed):
                print(f"    {t}")
        html_builder.add_run_section("✔ Починились", removed)

        print(f"⚠ Уникальные падения ({len(only_here)} шт.):" if only_here else "⚠ Уникальные падения: нет")
        if only_here:
            for t in sorted(only_here):
                print(f"    {t}")
        html_builder.add_run_section("⚠ Уникальные падения", only_here)

    # --- 4. Статистика --- #
    stats = results.get_statistics()
    print(f"\n📊 Статистика анализа:")
    print(f"   Всего запусков: {stats.get('total_runs', 0)}")
    print(f"   Уникальных падающих тестов: {stats.get('unique_failed_tests', 0)}")
    if BRANCH != MASTER_BRANCH:
        print(f"   Падает в master: {stats.get('master_failed_tests', 0)}")
        print(f"   Новые падения: {stats.get('new_failures', 0)}")

    # Добавляем статистику поведения тестов
    print(f"   Стабильно падающие: {len(stable_failing)}")
    print(f"   Починенные: {len(fixed_tests)}")
    print(f"   Нестабильные (flaky): {len(flaky_tests)}")

    # --- 5. Генерируем HTML --- #
    html_builder.write()


def main():
    if not GITHUB_TOKEN:
        print("❌ Переменная окружения GITHUB_TOKEN не задана.")
        return
    OUTPUT_DIR.mkdir(exist_ok=True)

    if SAVE_LOGS:
        print(f"💾 Режим сохранения txt файлов включён. Папка: {OUTPUT_DIR}")
    else:
        print("🗑 Режим сохранения логов отключён (SAVE_LOGS = False)")

    for repo in REPOS:
        try:
            analyse_repo(repo)
        except Exception as e:
            print(f"🔥 Ошибка при обработке {repo}: {e}")
            raise e


if __name__ == '__main__':
    main()
