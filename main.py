#!/usr/bin/env python3
import os
from pathlib import Path
from lib.html import HtmlReportBuilder
from lib.analyzer import GitHubWorkflowAnalyzer
from lib.analyze import TestAnalysisResults
import html

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
REPOS = ['hoper']  # <-- список репозиториев
# REPOS = ['hoper', 'hydra-server', "hydra-core", "hupo"]  # <-- список репозиториев
BRANCH = 'fix_features'  # Анализируемая ветка
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 25  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать txt и HTML
SAVE_LOGS = False  # Оставлять .txt на диске?
FORCE_REFRESH_CACHE = False  # Принудительно игнорировать кэш и переизвлекать результаты

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
    # Поддержка ENV переопределения: FORCE_REFRESH_CACHE=1|true|yes|on
    force_refresh_env = os.getenv('FORCE_REFRESH_CACHE', '').strip().lower() in ('1', 'true', 'yes', 'on')
    force_refresh = FORCE_REFRESH_CACHE or force_refresh_env
    analyzer.configure_cache(save_logs=SAVE_LOGS, log_save_dir=logs_dir, force_refresh_cache=force_refresh)
    if force_refresh:
        print("🧹 Режим принудительного обновления кэша включён (force_refresh_cache=True)")

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
    fixed_tests = behavior_analysis['fixed_tests']
    flaky_tests = behavior_analysis['flaky_tests']
    # Быстрый доступ к информации о тесте по имени
    behavior_map = {**stable_failing, **fixed_tests, **flaky_tests}
    print(f"\n🔴 Стабильно падающие тесты ({len(stable_failing)} шт.):")
    if stable_failing:
        stable_items = []
        for test_name, info in stable_failing.items():
            marker = "" if BRANCH == MASTER_BRANCH else (
                " (также в master)" if test_name in results.master_failed else " (только в ветке)"
            )
            first_fail = (info.get('failed_runs') or [{}])[0]
            first_meta = first_fail.get('meta', {})
            first_sha_full = first_fail.get('sha', '')
            ts = first_meta.get('ts', '')
            title = first_meta.get('title', '')
            # Вывод в консоль с датой/временем и заголовком коммита
            print(f"    • {test_name} — с {ts} — {title}{marker}")
            # HTML элемент списка с кнопкой скролла к соответствующему run
            run_anchor_id = f"run-{first_sha_full}" if first_sha_full else ""
            # Показываем только лист (последний сегмент после ::)
            leaf_name = test_name.split('::')[-1] if '::' in test_name else test_name
            label_text = f"{leaf_name}{marker} — с {ts} — {title}"
            label_safe = html.escape(label_text)
            button_html = f" <button onclick=\"scrollToRun('{run_anchor_id}')\">К запуску</button>" if run_anchor_id else ""
            stable_items.append({'display': label_safe + button_html, 'raw': test_name})
        html_builder.add_section("🔴 Стабильно падающие тесты", stable_items)
    else:
        print("    ✅ Нет стабильно падающих тестов")
        html_builder.add_section("🔴 Стабильно падающие тесты", ["✅ Нет стабильно падающих тестов"])

    # Починенные тесты
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
        # Порядки тестов в текущем и предыдущем ранах
        current_order = diff.get('order', [])
        prev_order = diff.get('prev_order', [])

        failed_total = len(diff.get('current', set()))
        # Добавляем число падений в метаданные, чтобы использовать в HTML-отчёте
        info['failed'] = failed_total
        # Прокидываем sha для привязки якорей секций run'ов
        info['sha'] = diff['sha']
        print(f"\n📦 {info['title']} | {info['ts']} | {info['concl']} | failed: {failed_total} | {info['link']}")

        # Начинаем новую секцию run'а в HTML
        html_builder.start_run_section(info)

        print(f"➕ Новые падения ({len(added)} шт.):" if added else "➕ Новые падения: нет")
        # Упорядочиваем по порядку текущего рана
        added_ordered = [t for t in current_order if t in added]
        if added_ordered:
            for t in added_ordered:
                marker = "" if BRANCH == MASTER_BRANCH else \
                    (" (также в master)" if t in results.master_failed else " (только здесь)")
                print(f"    {t}{marker}")
        def build_leaf_label(t: str, section: str) -> dict:
            # метки: также в master/только здесь — только для падений
            marker = ""
            if section in ("added", "only_here"):
                if BRANCH != MASTER_BRANCH:
                    marker = " (также в master)" if t in results.master_failed else " (только здесь)"
            # найдём первое падение этого теста
            binfo = behavior_map.get(t)
            ts = title = ""
            anchor_sha = None
            if binfo and binfo.get('failed_runs'):
                first_fail = binfo['failed_runs'][0]
                meta0 = first_fail.get('meta', {})
                ts = meta0.get('ts', '')
                title = meta0.get('title', '')
                anchor_sha = first_fail.get('sha')
            # Показываем только лист (последний сегмент после ::)
            leaf_name = t.split('::')[-1] if '::' in t else t
            label_text = f"{leaf_name}{marker} — с {ts} — {title}" if ts or title else f"{leaf_name}{marker}"
            label_safe = html.escape(label_text)
            button_html = f" <button onclick=\"scrollToRun('run-{anchor_sha}')\">К запуску</button>" if anchor_sha else ""
            return {'display': label_safe + button_html, 'raw': t}

        html_builder.add_run_section(
            "➕ Новые падения",
            [build_leaf_label(t, "added") for t in added_ordered]
        )

        print(f"✔ Починились ({len(removed)} шт.):" if removed else "✔ Починились: нет")
        # Упорядочиваем по порядку предыдущего рана
        removed_ordered = [t for t in prev_order if t in removed]
        if removed_ordered:
            for t in removed_ordered:
                print(f"    {t}")
        html_builder.add_run_section(
            "✔ Починились",
            [build_leaf_label(t, "removed") for t in removed_ordered]
        )

        print(f"⚠ Уникальные падения ({len(only_here)} шт.):" if only_here else "⚠ Уникальные падения: нет")
        # Упорядочиваем по порядку текущего рана
        only_here_ordered = [t for t in current_order if t in only_here]
        if only_here_ordered:
            for t in only_here_ordered:
                print(f"    {t}")
        html_builder.add_run_section(
            "⚠ Уникальные падения",
            [build_leaf_label(t, "only_here") for t in only_here_ordered]
        )

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
    # Сообщаем о режиме инвалидации кэша (по глобальному флагу или ENV)
    force_refresh_env = os.getenv('FORCE_REFRESH_CACHE', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if FORCE_REFRESH_CACHE or force_refresh_env:
        print("🧹 Инвалидация кэша включена (FORCE_REFRESH_CACHE)")

    for repo in REPOS:
        try:
            analyse_repo(repo)
        except Exception as e:
            print(f"🔥 Ошибка при обработке {repo}: {e}")
            raise e


if __name__ == '__main__':
    main()
