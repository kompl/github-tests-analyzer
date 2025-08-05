#!/usr/bin/env python3
import os
import re
import requests
import zipfile
import io
from pathlib import Path
from datetime import datetime

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
REPOS = ['hoper', 'hydra-server']  # <-- список репозиториев
BRANCH = 'v6.2'  # Анализируемая ветка
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 10  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать zip и HTML
SAVE_LOGS = True  # Оставлять .zip на диске?
PATTERNS = [
    re.compile(r"🧪\s*-\s*(.*?)\s*\|"),  # emoji-формат
    re.compile(r"\b(spec[^\s]+?\.rb(?:#L\d+)?)\b", re.I),  # spec/*.rb
    re.compile(r"\b(features[^\s]+?\.feature(?:#L\d+)?)\b", re.I),  # features/*.feature
]
HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}


# ====================================== #

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---------- #
def github_get(url, **kwargs):
    r = requests.get(url, headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r


def get_recent_runs(owner, repo, branch, workflow_file, max_runs):
    """
    Возвращает max_runs завершённых (success|failure) workflow-ранов,
    отсортированных от старого к новому.
    """
    collected = []
    page = 1
    per_page = 100  # максимум, чтобы быстрее пройти страницы
    while len(collected) < max_runs:
        url = f'https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs'
        params = {'branch': branch, 'per_page': per_page, 'page': page}
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        items = resp.json().get('workflow_runs', [])
        if not items:
            break  # ран-ов больше нет

        for run in items:
            if run['status'] == 'completed' and run.get('conclusion') in ('success', 'failure'):
                collected.append(run)
                if len(collected) == max_runs:
                    break
        page += 1

    # сортируем от старых к новым
    collected.sort(
        key=lambda x: datetime.fromisoformat(
            (x.get('run_started_at') or x.get('created_at')).replace('Z', '+00:00')
        )
    )
    return collected


def get_latest_completed_run(owner, repo, branch, workflow_file):
    """Возвращает последний COMPLETED run (success|failure)."""
    url = f'https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs'
    params = {'branch': branch, 'per_page': MAX_RUNS}
    for run in github_get(url, params=params).json().get('workflow_runs', []):
        if run['status'] == 'completed' and run.get('conclusion') in ('success', 'failure'):
            return run
    return None


def get_commit_title(owner, repo, sha):
    url = f'https://api.github.com/repos/{owner}/{repo}/commits/{sha}'
    return github_get(url).json().get('commit', {}).get('message', '').splitlines()[0]


def download_logs_bytes(owner, repo, run_id):
    url = f'https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs'
    try:
        return github_get(url).content
    except requests.HTTPError as e:
        print(f"⚠ Не могу скачать логи run {run_id}: {e}")
        return None


def parse_failed_tests_with_details(zip_bytes):
    """Ищем строки вида  🧪 - spec/... |  и собираем детали."""
    failed = {}  # path -> список строк с деталями
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith('.txt'):
                continue
            with z.open(name) as f:
                content_lines = []
                for raw in f:
                    line = raw.decode('utf-8', 'ignore')
                    content_lines.append(line)

                # Проходим по всем строкам и ищем совпадения
                for i, line in enumerate(content_lines):
                    for pat in PATTERNS:
                        m = pat.search(line)
                        if m:
                            path = m.group(1).strip()
                            # приводим вариант spec.a.b.c.rb → spec/a/b/c.rb
                            if ('/' not in path) and ('.' in path):
                                path = path.replace('.', '/')

                            # Собираем контекст вокруг найденной строки
                            context_start = max(0, i - 3)
                            context_end = min(len(content_lines), i + 10)
                            context = content_lines[context_start:context_end]

                            if path not in failed:
                                failed[path] = []
                            failed[path].append({
                                'file': name,
                                'line_num': i + 1,
                                'context': ''.join(context).strip()
                            })
    return failed


def parse_failed_tests(zip_bytes):
    """Возвращает только set путей для обратной совместимости."""
    details = parse_failed_tests_with_details(zip_bytes)
    return set(details.keys())


def print_list(items, indent=" • "):
    """Выводит полный список в консоль."""
    sorted_items = sorted(items)
    for item in sorted_items:
        print(indent + item)


class HtmlReportBuilder:
    def __init__(self, filename):
        self.filename = filename
        self.sections = []
        self.test_details = {}  # path -> детали теста
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)

    def add_test_details(self, test_details):
        """Добавляет детали тестов для использования в кнопках."""
        self.test_details.update(test_details)

    def add_section(self, title, items, max_show=10):
        total = len(items)
        sorted_items = sorted(items)
        if total == 0:
            content = f"<p>{title}: нет</p>"
        else:
            # Всегда показываем все тесты в спойлере
            items_html = []
            for item in sorted_items:
                # Убираем дополнительные пометки для создания ID кнопки
                clean_item = item.split(' (')[0] if ' (' in item else item
                detail_button = ""
                if clean_item in self.test_details:
                    detail_button = f" <button onclick=\"showDetails('{clean_item}')\">Подробно</button>"
                items_html.append(f'<li>{item}{detail_button}</li>')

            if total <= max_show:
                content = f"<p>{title} ({total} шт.):</p>\n<ul>" + ''.join(items_html) + '</ul>'
            else:
                content = f"""
<p>{title} ({total} шт.):</p>
<details>
<summary>Показать/скрыть список</summary>
<ul>
{''.join(items_html)}
</ul>
</details>
"""
        self.sections.append(content)

    def write(self):
        separator = '\n<hr>\n'

        # Создаем JavaScript для показа деталей
        details_js = "var testDetails = {\n"
        for test_path, details in self.test_details.items():
            details_text = ""
            for detail in details:
                details_text += f"Файл: {detail['file']}\\nСтрока: {detail['line_num']}\\n\\nКонтекст:\\n{detail['context']}\\n\\n---\\n\\n"
            # Экранируем для JavaScript
            details_text = details_text.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r',
                                                                                                               '\\r')
            details_js += f"  '{test_path}': '{details_text}',\n"
        details_js += "};\n"

        details_js += """
function showDetails(testPath) {
    var details = testDetails[testPath];
    if (details) {
        alert(details);
    } else {
        alert('Детали для теста ' + testPath + ' не найдены');
    }
}
"""

        html_content = f"""
<html>
<head>
    <meta charset='utf-8'>
    <title>Отчёт по упавшим тестам</title>
    <style>
        button {{
            background-color: #4CAF50;
            color: white;
            padding: 2px 8px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            font-size: 12px;
            margin-left: 10px;
        }}
        button:hover {{
            background-color: #45a049;
        }}
        details {{
            margin: 10px 0;
        }}
        summary {{
            cursor: pointer;
            font-weight: bold;
            padding: 5px;
            background-color: #f0f0f0;
            border-radius: 3px;
        }}
    </style>
    <script>
{details_js}
    </script>
</head>
<body>
{separator.join(self.sections)}
</body>
</html>
"""
        Path(self.filename).write_text(html_content, encoding='utf-8')
        print(f"✅ Сгенерирован HTML-отчёт: {self.filename}")


# --------------------------------------------- #

def analyse_repo(repo: str):
    print(f"\n================= 📁 Репозиторий: {repo} =================")
    html_builder = HtmlReportBuilder(OUTPUT_DIR / f'failed_tests_{repo}.html')

    # --- 1. Собираем падающие тесты в master, если нужно --- #
    master_failed = set()
    if BRANCH != MASTER_BRANCH:
        print(f"📦 Ищем последний завершённый run '{WORKFLOW_FILE}' в '{MASTER_BRANCH}'…")
        master_run = get_latest_completed_run(OWNER, repo, MASTER_BRANCH, WORKFLOW_FILE)
        if master_run:
            mb_zip = download_logs_bytes(OWNER, repo, master_run['id'])
            master_failed = parse_failed_tests(mb_zip) if mb_zip else set()
            print(f"✅ В {MASTER_BRANCH} упало {len(master_failed)} тестов.")
        else:
            print(f"⚠ Не найден завершённый run в {MASTER_BRANCH}.")
    else:
        print("ℹ Анализируется ветка master — сравнение с ней не требуется.")

    # --- 2. Получаем последние запуски нужной ветки --- #
    runs_raw = get_recent_runs(OWNER, repo, BRANCH, WORKFLOW_FILE, MAX_RUNS)
    runs = [r for r in runs_raw if r['status'] == 'completed']  # отбрасываем in_progress
    if not runs:
        print("❌ Завершённых запусков нет.")
        return

    # --- 3. Обрабатываем каждый run --- #
    summary, meta, all_test_details = {}, {}, {}  # sha -> set(failed) / мета-инфо / детали тестов
    for run in runs:
        sha = run['head_sha']
        title = get_commit_title(OWNER, repo, sha) or sha[:7]
        branch = run.get('head_branch')
        ts = datetime.fromisoformat(
            (run.get('run_started_at') or run.get('created_at')).replace('Z', '+00:00')
        ).strftime('%Y-%m-%d %H:%M:%S')
        concl = run.get('conclusion')
        run_link = f"https://github.com/{OWNER}/{repo}/actions/runs/{run['id']}"
        print(f"🔍 {title} | {branch} | {ts} | Статус: {concl} | {run_link}")

        zbytes = download_logs_bytes(OWNER, repo, run['id'])
        if zbytes:
            test_details = parse_failed_tests_with_details(zbytes)
            failed = set(test_details.keys())
            all_test_details.update(test_details)
        else:
            failed = set()

        summary[sha] = failed
        meta[sha] = {'title': title, 'ts': ts, 'concl': concl, 'link': run_link}

    # Добавляем все детали тестов в HTML builder
    html_builder.add_test_details(all_test_details)

    # --- 4. Выводим информацию о самом раннем run (ПОЛНЫЙ СПИСОК В КОНСОЛЬ) --- #
    first_sha = next(iter(summary))
    first_fail = summary[first_sha]
    print(f"\n=== 🐞 Тесты, упавшие в самом первом анализируемом запуске ({len(first_fail)} шт.) ===")
    if first_fail:
        for t in sorted(first_fail):
            marker = "" if BRANCH == MASTER_BRANCH else \
                (" (падает и в master)" if t in master_failed else " (не падает в master)")
            print(f" • {t}{marker}")
    else:
        print("✔ Падений нет")
    html_builder.add_section("Тесты, упавшие в самом первом анализируемом запуске",
                             [
                                 f"{t}{'' if BRANCH == MASTER_BRANCH else ' (падает и в master)' if t in master_failed else ' (не падает в master)'}"
                                 for t in first_fail])

    # --- 5. Дифф по запускам (ПОЛНЫЙ СПИСОК В КОНСОЛЬ) --- #
    print("\n=== 📊 Изменения падений тестов по последним запускам ===")
    prev = set()
    for sha, curr in summary.items():
        added = curr - prev
        removed = prev - curr
        info = meta[sha]
        print(f"\n📦 {info['title']} | {info['ts']} | {info['concl']} | {info['link']}")

        print(f"➕ Новые падения ({len(added)} шт.):" if added else "➕ Новые падения: нет")
        if added:
            for t in sorted(added):
                marker = "" if BRANCH == MASTER_BRANCH else \
                    (" (также в master)" if t in master_failed else " (только здесь)")
                print(f"    {t}{marker}")
        html_builder.add_section(f"Новые падения в {info['title']}",
                                 [
                                     f"{t}{'' if BRANCH == MASTER_BRANCH else ' (также в master)' if t in master_failed else ' (только здесь)'}"
                                     for t in added])

        print(f"✔ Починились ({len(removed)} шт.):" if removed else "✔ Починились: нет")
        if removed:
            for t in sorted(removed):
                print(f"    {t}")
        html_builder.add_section(f"Починились в {info['title']}", removed)

        only_here = curr - master_failed if BRANCH != MASTER_BRANCH else set()
        print(f"⚠ Уникальные падения ({len(only_here)} шт.):" if only_here else "⚠ Уникальные падения: нет")
        if only_here:
            for t in sorted(only_here):
                print(f"    {t}")
        html_builder.add_section(f"Уникальные падения в {info['title']}", only_here)

        prev = curr

    # --- 6. Генерируем HTML в конце --- #
    html_builder.write()


def main():
    if not GITHUB_TOKEN:
        print("❌ Переменная окружения GITHUB_TOKEN не задана.")
        return
    OUTPUT_DIR.mkdir(exist_ok=True)
    for repo in REPOS:
        try:
            analyse_repo(repo)
        except Exception as e:
            print(f"🔥 Ошибка при обработке {repo}: {e}")


if __name__ == '__main__':
    main()
