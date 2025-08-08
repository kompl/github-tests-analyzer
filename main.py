#!/usr/bin/env python3
import os
from pathlib import Path
from lib.html import HtmlReportBuilder
from lib.cache import ArtifactCache
from lib.analyze import GitHubWorkflowAnalyzer, TestAnalysisResults

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
REPOS = ['hupo']  # <-- список репозиториев
# REPOS = ['hoper', 'hydra-server', "hydra-core", "hupo"]  # <-- список репозиториев
BRANCH = 'master'  # Анализируемая ветка
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 12  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать txt и HTML
CACHE_DIR = OUTPUT_DIR / 'cache'  # Директория для кэша артефактов
SAVE_LOGS = False  # Оставлять .txt на диске?

# Инициализируем кэш директорию
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_METADATA_FILE = CACHE_DIR / 'metadata.json'

# Глобальный объект кэша
artifact_cache = ArtifactCache(CACHE_DIR, CACHE_METADATA_FILE)


def download_logs_bytes(analyzer, repo, run_id, save_dir=None, run_prefix="", run_info=None):
    """Скачивает логи с использованием кэша и опционально сохраняет txt файлы на диск."""
    # Проверяем кэш
    if artifact_cache.has_cached(OWNER, repo, run_id):
        zip_bytes = artifact_cache.get_cached(OWNER, repo, run_id)
        if zip_bytes is None:
            print(f"⚠ Ошибка чтения кэшированного артефакта для run {run_id}")
        else:
            # Сохраняем txt файлы, если нужно
            if save_dir and SAVE_LOGS and zip_bytes:
                _save_txt_files_from_zip(zip_bytes, save_dir, run_prefix)
            return zip_bytes

    # Скачиваем из API
    zip_bytes = analyzer.download_logs(repo, run_id)
    if zip_bytes:
        print(f"⬇️ Скачиваем новый артефакт для run {run_id}")

        # Сохраняем в кэш
        cache_stored = artifact_cache.store_artifact(OWNER, repo, run_id, zip_bytes, run_info)
        if cache_stored:
            print(f"💾 Артефакт сохранён в кэш для run {run_id}")

        # Сохраняем txt файлы, если указана директория
        if save_dir and SAVE_LOGS:
            _save_txt_files_from_zip(zip_bytes, save_dir, run_prefix)

    return zip_bytes


def _save_txt_files_from_zip(zip_bytes, save_dir, run_prefix):
    """Извлекает и сохраняет txt файлы из zip архива."""
    import zipfile
    import io

    if not zip_bytes:
        return 0

    save_dir.mkdir(parents=True, exist_ok=True)
    saved_count = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for name in z.namelist():
                if name.lower().endswith('.txt'):
                    # Создаём безопасное имя файла
                    safe_name = name.replace('/', '_').replace('\\', '_')
                    if run_prefix:
                        safe_name = f"{run_prefix}_{safe_name}"

                    txt_path = save_dir / safe_name

                    # Извлекаем и сохраняем содержимое
                    with z.open(name) as f:
                        content = f.read()
                        txt_path.write_bytes(content)
                        saved_count += 1

        if saved_count > 0:
            print(f"💾 Сохранено {saved_count} txt файлов в {save_dir}")
    except Exception as e:
        print(f"⚠ Ошибка сохранения txt файлов: {e}")

    return saved_count


def analyse_repo(repo: str):
    """Анализирует репозиторий с использованием нового анализатора."""
    print(f"\n================= 📁 Репозиторий: {repo} =================")

    # Инициализируем анализатор и объект результатов
    analyzer = GitHubWorkflowAnalyzer(GITHUB_TOKEN, OWNER, WORKFLOW_FILE)
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

    # --- 1. Получаем падающие тесты в master, если нужно --- #
    if BRANCH != MASTER_BRANCH:
        print(f"📦 Ищем последний завершённый run '{WORKFLOW_FILE}' в '{MASTER_BRANCH}'…")
        master_failed = analyzer.get_master_failed_tests(repo, MASTER_BRANCH)
        results.set_master_failed(master_failed)
        print(f"✅ В {MASTER_BRANCH} упало {len(master_failed)} тестов.")
    else:
        print("ℹ Анализируется ветка master — сравнение с ней не требуется.")

    # --- 2. Анализируем запуски --- #
    summary, meta, all_test_details = analyzer.analyze_repo_runs(repo, BRANCH, MAX_RUNS)
    results.add_run_data(summary, meta, all_test_details)

    if not summary:
        print("❌ Завершённых запусков нет.")
        return

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
            print(f"    • {test_name} (последнее падение: {info['failed_runs'][-1]['']}-й запуск)")
        html_builder.add_section("✅ Починенные тесты",
                                 [f"{test} (последнее падение: {info['last_fail_run']}-й запуск)"
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

    # Выводим информацию о кэше
    cache_stats = artifact_cache.get_cache_stats()
    print(f"📂 Кэш артефактов: {cache_stats['total_cached']} файлов ({cache_stats['total_size_mb']} МБ)")
    print(f"   Директория: {cache_stats['cache_dir']}")

    # Очищаем потерянные файлы кэша
    cleaned = artifact_cache.cleanup_orphaned()
    if cleaned > 0:
        print(f"🧹 Очищено {cleaned} потерянных файлов кэша")

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

    # Показываем финальную статистику кэша
    final_stats = artifact_cache.get_cache_stats()
    print(
        f"\n📊 Финальная статистика кэша: {final_stats['total_cached']} артефактов ({final_stats['total_size_mb']} МБ)")


if __name__ == '__main__':
    main()
