import os
from pathlib import Path
from typing import Optional, Dict, List, Any
import html

from .html import HtmlReportBuilder
from .analyzer import GitHubWorkflowAnalyzer
from .analyze import TestAnalysisResults


class ReportService:
    """
    Сервис построения отчёта по упавшим тестам.

    Инкапсулирует логику из main.py: извлечение данных, анализ поведения,
    построение секций и генерация HTML отчёта.
    """

    def __init__(self, github_token: str, owner: str, workflow_file: str = 'ci.yml') -> None:
        self.github_token = github_token
        self.owner = owner
        self.workflow_file = workflow_file
        self.analyzer = GitHubWorkflowAnalyzer(github_token, owner, workflow_file=workflow_file)

    def analyze_repo(
        self,
        repo: str,
        branch: str,
        master_branch: str,
        max_runs: int,
        output_dir: Path,
        save_logs: bool,
        force_refresh_default: bool,
    ) -> Optional[Path]:
        """
        Выполняет анализ репозитория и генерирует HTML-отчёт. Возвращает путь к отчёту или None.
        """
        print(f"\n================= 📁 Репозиторий: {repo} =================")

        results = TestAnalysisResults(repo, branch)

        # Директория и HTML builder
        branch_output_dir = output_dir / branch.replace('/', '_')
        branch_output_dir.mkdir(parents=True, exist_ok=True)
        report_path = branch_output_dir / f'failed_tests_{repo}.html'
        html_builder = HtmlReportBuilder(report_path, repo, branch)

        # Папка для логов
        logs_dir = output_dir / f'{repo}_logs'
        if save_logs:
            logs_dir.mkdir(parents=True, exist_ok=True)

        # Настройки кэша и логов
        force_refresh_env = os.getenv('FORCE_REFRESH_CACHE', '').strip().lower() in ('1', 'true', 'yes', 'on')
        force_refresh = force_refresh_default or force_refresh_env
        self.analyzer.configure_cache(save_logs=save_logs, log_save_dir=logs_dir, force_refresh_cache=force_refresh)
        if force_refresh:
            print("🧹 Режим принудительного обновления кэша включён (force_refresh_cache=True)")

        # Падающие тесты в master
        master_failed = set()
        if branch != master_branch:
            print(f"📦 Ищем последний завершённый run '{self.workflow_file}' в '{master_branch}'…")
            master_failed = self.analyzer.get_master_failed_tests(repo, master_branch)
            print(f"✅ В {master_branch} упало {len(master_failed)} тестов.")
        results.set_master_failed(master_failed)

        # Анализ последних запусков ветки
        summary, meta, all_test_details = self.analyzer.analyze_repo_runs(repo, branch, max_runs)
        results.add_run_data(summary, meta, all_test_details)

        if not summary:
            print("❌ Завершённых запусков нет.")
            return None

        # Детали в HTML
        html_builder.add_test_details(all_test_details)

        # Анализ поведения
        behavior_analysis = results.analyze_test_behavior()

        print(f"\n=== 🔍 Анализ поведения тестов в {len(summary)} запусках ===")

        # Быстрый доступ к инфо
        stable_failing = behavior_analysis['stable_failing']
        fixed_tests = behavior_analysis['fixed_tests']
        flaky_tests = behavior_analysis['flaky_tests']
        behavior_map = {**stable_failing, **fixed_tests, **flaky_tests}

        # Стабильно падающие
        print(f"\n🔴 Стабильно падающие тесты ({len(stable_failing)} шт.):")
        if stable_failing:
            stable_with_pos = []  # (pos, display_raw_dict)
            for test_name, info in stable_failing.items():
                marker = "" if branch == master_branch else (
                    " (также в master)" if test_name in results.master_failed else " (только в ветке)"
                )
                first_fail = (info.get('failed_runs') or [{}])[0]
                first_meta = first_fail.get('meta', {})
                first_sha_full = first_fail.get('sha', '')
                ts = first_meta.get('ts', '')
                title = first_meta.get('title', '')
                print(f"    • {test_name} — с {ts} — {title}{marker}")
                run_anchor_id = f"run-{first_sha_full}" if first_sha_full else ""
                leaf_name = test_name.split('::')[-1] if '::' in test_name else test_name
                label_text = f"{leaf_name}{marker} — с {ts} — {title}"
                label_safe = html.escape(label_text)
                button_html = f" <button onclick=\"scrollToRun('{run_anchor_id}')\">К запуску</button>" if run_anchor_id else ""
                item_obj = {'display': label_safe + button_html, 'raw': test_name}
                pos = 10**9
                order_list = results.meta.get(first_sha_full, {}).get('order', [])
                if order_list and test_name in order_list:
                    try:
                        pos = order_list.index(test_name)
                    except ValueError:
                        pos = 10**9
                stable_with_pos.append((pos, item_obj))
            stable_with_pos.sort(key=lambda x: x[0])
            stable_items = [obj for _, obj in stable_with_pos]
            html_builder.add_section("🔴 Стабильно падающие тесты", stable_items)
        else:
            print("    ✅ Нет стабильно падающих тестов")
            html_builder.add_section("🔴 Стабильно падающие тесты", ["✅ Нет стабильно падающих тестов"])

        # Починенные
        print(f"\n✅ Починенные тесты ({len(fixed_tests)} шт.):")
        if fixed_tests:
            for test_name, info in fixed_tests.items():
                print(f"    • {test_name} (починено в {info['next_commit_info']['title']}: {info['next_pr_link']})")
            html_builder.add_section(
                "✅ Починенные тесты",
                [f"{test} (починено в {info['next_commit_info']['title']}: <href>{info['next_pr_link']}</href>)"
                 for test, info in fixed_tests.items()]
            )
        else:
            print("    ❌ Нет починенных тестов")
            html_builder.add_section("✅ Починенные тесты", ["❌ Нет починенных тестов"])

        # Flaky
        print(f"\n🟡 Нестабильные (flaky) тесты ({len(flaky_tests)} шт.):")
        if flaky_tests:
            for test_name, info in flaky_tests.items():
                pattern = info['pattern']
                fail_rate = (info['fail_count'] / info['total_runs']) * 100
                print(f"    • {test_name} (паттерн: {pattern}, падает {fail_rate:.1f}% времени)")
            html_builder.add_section(
                "🟡 Нестабильные (flaky) тесты",
                [
                    f"{test} (паттерн: {info['pattern']}, падает {(info['fail_count'] / info['total_runs']) * 100:.1f}% времени)"
                    for test, info in flaky_tests.items()
                ]
            )
        else:
            print("    ✅ Нет нестабильных тестов")
            html_builder.add_section("🟡 Нестабильные (flaky) тесты", ["✅ Нет нестабильных тестов"])

        # Диффы по запускам
        print("\n=== 📊 Изменения падений тестов по последним запускам ===")
        for diff in results.get_run_diffs():
            info = diff['meta']
            added = diff['added']
            removed = diff['removed']
            only_here = diff['only_here']
            current_order = diff.get('order', [])
            prev_order = diff.get('prev_order', [])

            failed_total = len(diff.get('current', set()))
            info['failed'] = failed_total
            info['sha'] = diff['sha']
            print(f"\n📦 {info['title']} | {info['ts']} | {info['concl']} | failed: {failed_total} | {info['link']}")

            html_builder.start_run_section(info)

            print(f"➕ Новые падения ({len(added)} шт.):" if added else "➕ Новые падения: нет")
            added_ordered = [t for t in current_order if t in added]
            if added_ordered:
                for t in added_ordered:
                    marker = "" if branch == master_branch else (
                        " (также в master)" if t in results.master_failed else " (только здесь)"
                    )
                    print(f"    {t}{marker}")

            def build_leaf_label(t: str, section: str) -> dict:
                marker = ""
                if section in ("added", "only_here") and branch != master_branch:
                    marker = " (также в master)" if t in results.master_failed else " (только здесь)"
                binfo = behavior_map.get(t)
                ts = title = ""
                anchor_sha = None
                if binfo and binfo.get('failed_runs'):
                    first_fail = binfo['failed_runs'][0]
                    meta0 = first_fail.get('meta', {})
                    ts = meta0.get('ts', '')
                    title = meta0.get('title', '')
                    anchor_sha = first_fail.get('sha')
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
            removed_ordered = [t for t in prev_order if t in removed]
            if removed_ordered:
                for t in removed_ordered:
                    print(f"    {t}")
            html_builder.add_run_section(
                "✔ Починились",
                [build_leaf_label(t, "removed") for t in removed_ordered]
            )

            print(f"⚠ Уникальные падения ({len(only_here)} шт.):" if only_here else "⚠ Уникальные падения: нет")
            only_here_ordered = [t for t in current_order if t in only_here]
            if only_here_ordered:
                for t in only_here_ordered:
                    print(f"    {t}")
            html_builder.add_run_section(
                "⚠ Уникальные падения",
                [build_leaf_label(t, "only_here") for t in only_here_ordered]
            )

            # Полный список текущих падений (свернутый по умолчанию)
            all_current = list(diff.get('current', set()) or [])
            all_ordered = [t for t in current_order if t in all_current]
            html_builder.add_run_section(
                "📋 Все падения",
                [build_leaf_label(t, "current") for t in all_ordered],
                max_show=0  # всегда в <details>
            )

        # Статистика
        stats = results.get_statistics()
        print(f"\n📊 Статистика анализа:")
        print(f"   Всего запусков: {stats.get('total_runs', 0)}")
        print(f"   Уникальных падающих тестов: {stats.get('unique_failed_tests', 0)}")
        if branch != master_branch:
            print(f"   Падает в master: {stats.get('master_failed_tests', 0)}")
            print(f"   Новые падения: {stats.get('new_failures', 0)}")
        print(f"   Стабильно падающие: {len(stable_failing)}")
        print(f"   Починенные: {len(fixed_tests)}")
        print(f"   Нестабильные (flaky): {len(flaky_tests)}")

        # Генерация HTML
        html_builder.write()
        return report_path
