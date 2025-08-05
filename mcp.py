#!/usr/bin/env python3
import os
import re
import requests
import zipfile
import io
from pathlib import Path
from datetime import datetime
import hashlib
import json

# ============ Конфигурация ============ #
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'  # Организация / пользователь
REPOS = ['hoper', 'hydra-server']  # <-- список репозиториев
BRANCH = 'v6.2'  # Анализируемая ветка
MASTER_BRANCH = 'master'  # Ветка-эталон
WORKFLOW_FILE = 'ci.yml'  # Запускаемый workflow
MAX_RUNS = 10  # Сколько запусков анализируем
OUTPUT_DIR = Path('downloaded_logs')  # Куда складывать txt и HTML
CACHE_DIR = OUTPUT_DIR / 'cache'  # Директория для кэша артефактов
SAVE_LOGS = False  # Оставлять .txt на диске?
PATTERNS = [
    re.compile(r"🧪\s*-\s*(.*?)\s*\|"),  # emoji-формат
    re.compile(r"\b(spec[^\s]+?\.rb(?:#L\d+)?)\b", re.I),  # spec/*.rb
    re.compile(r"\b(features[^\s]+?\.feature(?:#L\d+)?)\b", re.I),  # features/*.feature
]
HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}

# Инициализируем кэш директорию
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_METADATA_FILE = CACHE_DIR / 'metadata.json'


# ====================================== #

# ---------- КЭШИРОВАНИЕ ---------- #
class ArtifactCache:
    def __init__(self, cache_dir, metadata_file):
        self.cache_dir = Path(cache_dir)
        self.metadata_file = Path(metadata_file)
        self.metadata = self._load_metadata()

    def _load_metadata(self):
        """Загружает метаданные кэша."""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"⚠ Ошибка загрузки метаданных кэша: {e}")
        return {}

    def _save_metadata(self):
        """Сохраняет метаданные кэша."""
        try:
            with open(self.metadata_file, 'w', encoding='utf-8') as f:
                json.dump(self.metadata, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"⚠ Ошибка сохранения метаданных кэша: {e}")

    def _get_cache_key(self, owner, repo, run_id):
        """Генерирует ключ кэша."""
        return f"{owner}_{repo}_{run_id}"

    def _get_cache_path(self, cache_key):
        """Возвращает путь к кэшированному файлу."""
        return self.cache_dir / f"{cache_key}.zip"

    def has_cached(self, owner, repo, run_id):
        """Проверяет, есть ли артефакт в кэше."""
        cache_key = self._get_cache_key(owner, repo, run_id)
        cache_path = self._get_cache_path(cache_key)
        return cache_path.exists() and cache_key in self.metadata

    def get_cached(self, owner, repo, run_id):
        """Возвращает кэшированный артефакт."""
        cache_key = self._get_cache_key(owner, repo, run_id)
        cache_path = self._get_cache_path(cache_key)

        if cache_path.exists():
            try:
                return cache_path.read_bytes()
            except IOError as e:
                print(f"⚠ Ошибка чтения кэшированного файла {cache_path}: {e}")
                return None
        return None

    def store_artifact(self, owner, repo, run_id, zip_bytes, run_info=None):
        """Сохраняет артефакт в кэш."""
        cache_key = self._get_cache_key(owner, repo, run_id)
        cache_path = self._get_cache_path(cache_key)

        try:
            cache_path.write_bytes(zip_bytes)

            # Обновляем метаданные
            self.metadata[cache_key] = {
                'owner': owner,
                'repo': repo,
                'run_id': run_id,
                'cached_at': datetime.now().isoformat(),
                'size_bytes': len(zip_bytes),
                'run_info': run_info or {}
            }
            self._save_metadata()

            return True
        except IOError as e:
            print(f"⚠ Ошибка сохранения в кэш {cache_path}: {e}")
            return False

    def get_cache_stats(self):
        """Возвращает статистику кэша."""
        total_files = len(self.metadata)
        total_size = sum(item.get('size_bytes', 0) for item in self.metadata.values())

        # Проверяем актуальность файлов
        actual_files = len([p for p in self.cache_dir.glob("*.zip") if p.exists()])

        return {
            'total_cached': total_files,
            'actual_files': actual_files,
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'cache_dir': str(self.cache_dir)
        }

    def cleanup_orphaned(self):
        """Удаляет файлы кэша без метаданных."""
        cleaned = 0
        for zip_file in self.cache_dir.glob("*.zip"):
            cache_key = zip_file.stem
            if cache_key not in self.metadata:
                try:
                    zip_file.unlink()
                    cleaned += 1
                except OSError:
                    pass
        return cleaned


# Глобальный объект кэша
artifact_cache = ArtifactCache(CACHE_DIR, CACHE_METADATA_FILE)


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


def download_logs_bytes(owner, repo, run_id, save_dir=None, run_prefix="", run_info=None):
    """Скачивает логи с использованием кэша и опционально сохраняет txt файлы на диск."""

    # Проверяем кэш
    if artifact_cache.has_cached(owner, repo, run_id):
        print(f"📂 Используем кэшированный артефакт для run {run_id}")
        zip_bytes = artifact_cache.get_cached(owner, repo, run_id)
        if zip_bytes is None:
            print(f"⚠ Ошибка чтения кэшированного артефакта для run {run_id}")
        else:
            # Сохраняем txt файлы, если нужно
            if save_dir and SAVE_LOGS and zip_bytes:
                _save_txt_files_from_zip(zip_bytes, save_dir, run_prefix)
            return zip_bytes

    # Скачиваем из API
    url = f'https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs'
    try:
        print(f"⬇️ Скачиваем новый артефакт для run {run_id}")
        response = github_get(url)
        zip_bytes = response.content

        # Сохраняем в кэш
        cache_stored = artifact_cache.store_artifact(owner, repo, run_id, zip_bytes, run_info)
        if cache_stored:
            print(f"💾 Артефакт сохранён в кэш для run {run_id}")

        # Сохраняем txt файлы, если указана директория
        if save_dir and SAVE_LOGS:
            _save_txt_files_from_zip(zip_bytes, save_dir, run_prefix)

        return zip_bytes
    except requests.HTTPError as e:
        print(f"⚠ Не могу скачать логи run {run_id}: {e}")
        return None


def _save_txt_files_from_zip(zip_bytes, save_dir, run_prefix):
    """Извлекает и сохраняет txt файлы из zip архива."""
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
    def __init__(self, filename, repo_name, branch_name):
        self.filename = filename
        self.repo_name = repo_name
        self.branch_name = branch_name
        self.sections = []
        self.test_details = {}  # path -> детали теста
        self.counter = 0  # Счетчик для уникальных ID
        self.current_run_sections = []  # Секции текущего run'а
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)

    def add_test_details(self, test_details):
        """Добавляет детали тестов для использования в кнопках."""
        self.test_details.update(test_details)

    def start_run_section(self, commit_info):
        """Начинает новую секцию для run'а."""
        if self.current_run_sections:
            # Завершаем предыдущий run
            self.finish_run_section()

        # Создаём заголовок для нового run'а
        run_header = f"""
<div class="run-section">
    <div class="run-header">
        <h3>🚀 {commit_info['title']}</h3>
        <div class="run-meta">
            <span class="run-branch">Ветка: <strong>{commit_info['branch']}</strong></span>
            <span class="run-date">Дата: <strong>{commit_info['ts']}</strong></span>
            <span class="run-status status-{commit_info['concl']}">Статус: <strong>{commit_info['concl']}</strong></span>
            <span class="run-link"><a href="{commit_info['link']}" target="_blank">🔗 Посмотреть в GitHub</a></span>
        </div>
    </div>
    <div class="run-content">
"""
        self.current_run_sections = [run_header]

    def add_run_section(self, title, items, max_show=10):
        """Добавляет секцию в текущий run."""
        total = len(items)
        sorted_items = sorted(items)

        if total == 0:
            content = f"<div class=\"test-section\"><h4>{title}:</h4><p class=\"no-tests\">нет</p></div>"
        else:
            # Всегда показываем все тесты в спойлере
            items_html = []
            for item in sorted_items:
                # Убираем дополнительные пометки для создания ID кнопки
                clean_item = item.split(' (')[0] if ' (' in item else item
                detail_button = ""
                if clean_item in self.test_details:
                    self.counter += 1
                    detail_id = f"details_{self.counter}"
                    detail_button = f" <button onclick=\"toggleDetails('{clean_item}', '{detail_id}')\">Подробно</button>"
                    detail_button += f"<div id=\"{detail_id}\" class=\"test-details\" style=\"display: none;\"></div>"
                items_html.append(f'<li>{item}{detail_button}</li>')

            if total <= max_show:
                content = f"""
<div class="test-section">
    <h4>{title} ({total} шт.):</h4>
    <ul class="test-list">{''.join(items_html)}</ul>
</div>"""
            else:
                content = f"""
<div class="test-section">
    <h4>{title} ({total} шт.):</h4>
    <details>
        <summary>Показать/скрыть список</summary>
        <ul class="test-list">{''.join(items_html)}</ul>
    </details>
</div>"""

        self.current_run_sections.append(content)

    def finish_run_section(self):
        """Завершает текущую секцию run'а."""
        if self.current_run_sections:
            self.current_run_sections.append("</div></div>")  # Закрываем run-content и run-section
            self.sections.extend(self.current_run_sections)
            self.current_run_sections = []

    def add_section(self, title, items, max_show=10, commit_info=None):
        """Добавляет обычную секцию (для первого запуска)."""
        total = len(items)
        sorted_items = sorted(items)

        # Добавляем информацию о коммите в заголовок
        section_title = title
        if commit_info:
            section_title += f" | {commit_info['branch']} | {commit_info['ts']} | <a href=\"{commit_info['link']}\" target=\"_blank\">{commit_info['title']}</a>"

        if total == 0:
            content = f"<p>{section_title}: нет</p>"
        else:
            # Всегда показываем все тесты в спойлере
            items_html = []
            for item in sorted_items:
                # Убираем дополнительные пометки для создания ID кнопки
                clean_item = item.split(' (')[0] if ' (' in item else item
                detail_button = ""
                if clean_item in self.test_details:
                    self.counter += 1
                    detail_id = f"details_{self.counter}"
                    detail_button = f" <button onclick=\"toggleDetails('{clean_item}', '{detail_id}')\">Подробно</button>"
                    detail_button += f"<div id=\"{detail_id}\" class=\"test-details\" style=\"display: none;\"></div>"
                items_html.append(f'<li>{item}{detail_button}</li>')

            if total <= max_show:
                content = f"<p>{section_title} ({total} шт.):</p>\n<ul>" + ''.join(items_html) + '</ul>'
            else:
                content = f"""
<p>{section_title} ({total} шт.):</p>
<details>
<summary>Показать/скрыть список</summary>
<ul>
{''.join(items_html)}
</ul>
</details>
"""
        self.sections.append(content)

    def write(self):
        # Завершаем последний run, если есть
        if self.current_run_sections:
            self.finish_run_section()

        separator = '\n<hr>\n'

        # Создаем JavaScript для показа деталей
        details_js = "var testDetails = {\n"
        for test_path, details in self.test_details.items():
            details_text = ""
            for detail in details:
                details_text += f"Файл: {detail['file']}\\nСтрока: {detail['line_num']}\\n\\nКонтекст:\\n{detail['context']}\\n\\n---\\n\\n"
            # Правильное экранирование для JavaScript
            details_text = details_text.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r',
                                                                                                               '\\r')
            details_js += f"  '{test_path}': '{details_text}',\n"
        details_js += "};\n"

        details_js += """
function toggleDetails(testPath, detailId) {
    var detailDiv = document.getElementById(detailId);
    if (detailDiv.style.display === 'none') {
        var details = testDetails[testPath];
        if (details) {
            // Заменяем экранированные \\n на реальные переносы строк для отображения
            var formattedDetails = details.replace(/\\\\n/g, '\\n');
            detailDiv.innerHTML = '<pre>' + formattedDetails + '</pre>';
            detailDiv.style.display = 'block';
        } else {
            detailDiv.innerHTML = '<p>Детали для теста ' + testPath + ' не найдены</p>';
            detailDiv.style.display = 'block';
        }
    } else {
        detailDiv.style.display = 'none';
    }
}
"""

        # Генерируем заголовок с информацией о репозитории и ветке
        report_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cache_stats = artifact_cache.get_cache_stats()
        header = f"""
<h1>Отчёт по упавшим тестам</h1>
<div class="report-info">
    <p><strong>Репозиторий:</strong> {self.repo_name}</p>
    <p><strong>Ветка:</strong> {self.branch_name}</p>
    <p><strong>Дата генерации:</strong> {report_date}</p>
    <p><strong>Кэш:</strong> {cache_stats['total_cached']} артефактов ({cache_stats['total_size_mb']} МБ)</p>
</div>
"""

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset='utf-8'>
    <title>Отчёт по упавшим тестам - {self.repo_name} ({self.branch_name})</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
        }}
        h1 {{
            color: #333;
            border-bottom: 2px solid #4CAF50;
            padding-bottom: 10px;
        }}
        .report-info {{
            background-color: #f9f9f9;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .report-info p {{
            margin: 5px 0;
        }}

        /* Стили для секций run'ов */
        .run-section {{
            margin: 20px 0;
            border: 1px solid #ddd;
            border-radius: 8px;
            overflow: hidden;
        }}
        .run-header {{
            background: linear-gradient(135deg, #4CAF50, #45a049);
            color: white;
            padding: 15px;
        }}
        .run-header h3 {{
            margin: 0 0 10px 0;
            font-size: 18px;
        }}
        .run-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            font-size: 14px;
        }}
        .run-meta span {{
            background: rgba(255,255,255,0.2);
            padding: 4px 8px;
            border-radius: 3px;
        }}
        .run-meta a {{
            color: white;
            text-decoration: none;
        }}
        .run-meta a:hover {{
            text-decoration: underline;
        }}
        .status-success {{
            background: rgba(76, 175, 80, 0.3) !important;
        }}
        .status-failure {{
            background: rgba(244, 67, 54, 0.3) !important;
        }}
        .run-content {{
            padding: 20px;
            background: #fafafa;
        }}
        .test-section {{
            margin: 15px 0;
            padding: 15px;
            background: white;
            border-radius: 5px;
            border-left: 4px solid #4CAF50;
        }}
        .test-section h4 {{
            margin: 0 0 10px 0;
            color: #333;
            font-size: 16px;
        }}
        .no-tests {{
            color: #666;
            font-style: italic;
        }}
        .test-list {{
            margin: 10px 0;
        }}
        .test-list li {{
            margin: 8px 0;
            padding: 8px;
            background: #f9f9f9;
            border-radius: 3px;
            border-left: 3px solid #4CAF50;
        }}

        button {{
            background-color: #4CAF50;
            color: white;
            padding: 4px 12px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            font-size: 12px;
            margin-left: 10px;
        }}
        button:hover {{
            background-color: #45a049;
        }}
        .test-details {{
            background-color: #f5f5f5;
            border: 1px solid #ddd;
            border-radius: 3px;
            padding: 10px;
            margin: 10px 0;
            max-height: 400px;
            overflow-y: auto;
        }}
        .test-details pre {{
            margin: 0;
            white-space: pre-wrap;
            word-wrap: break-word;
            font-family: 'Courier New', monospace;
            font-size: 12px;
        }}
        details {{
            margin: 10px 0;
        }}
        summary {{
            cursor: pointer;
            font-weight: bold;
            padding: 8px;
            background-color: #f0f0f0;
            border-radius: 3px;
            margin-bottom: 5px;
        }}
        summary:hover {{
            background-color: #e0e0e0;
        }}
        ul {{
            margin: 10px 0;
        }}
        li {{
            margin: 8px 0;
            padding: 5px;
            border-left: 3px solid #4CAF50;
            padding-left: 10px;
        }}
        hr {{
            border: 0;
            height: 2px;
            background: linear-gradient(to right, transparent, #ccc, transparent);
            margin: 20px 0;
        }}
        a {{
            color: #4CAF50;
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
        p {{
            margin: 10px 0;
        }}

        @media (max-width: 768px) {{
            .run-meta {{
                flex-direction: column;
                gap: 8px;
            }}
        }}
    </style>
</head>
<body>
{header}
{separator.join(self.sections)}

<script>
{details_js}
</script>
</body>
</html>
"""
        Path(self.filename).write_text(html_content, encoding='utf-8')
        print(f"✅ Сгенерирован HTML-отчёт: {self.filename}")


# --------------------------------------------- #

def analyse_repo(repo: str):
    print(f"\n================= 📁 Репозиторий: {repo} =================")
    html_builder = HtmlReportBuilder(OUTPUT_DIR / f'failed_tests_{repo}.html', repo, BRANCH)

    # Создаём папку для логов конкретного репозитория
    logs_dir = OUTPUT_DIR / f'{repo}_logs'
    if SAVE_LOGS:
        logs_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Собираем падающие тесты в master, если нужно --- #
    master_failed = set()
    if BRANCH != MASTER_BRANCH:
        print(f"📦 Ищем последний завершённый run '{WORKFLOW_FILE}' в '{MASTER_BRANCH}'…")
        master_run = get_latest_completed_run(OWNER, repo, MASTER_BRANCH, WORKFLOW_FILE)
        if master_run:
            master_save_dir = logs_dir if SAVE_LOGS else None
            run_info = {
                'branch': MASTER_BRANCH,
                'title': get_commit_title(OWNER, repo, master_run['head_sha']),
                'conclusion': master_run.get('conclusion')
            }
            mb_zip = download_logs_bytes(OWNER, repo, master_run['id'], master_save_dir, "master", run_info)
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
    for i, run in enumerate(runs):
        sha = run['head_sha']
        title = get_commit_title(OWNER, repo, sha) or sha[:7]
        branch = run.get('head_branch')
        ts = datetime.fromisoformat(
            (run.get('run_started_at') or run.get('created_at')).replace('Z', '+00:00')
        ).strftime('%Y-%m-%d %H:%M:%S')
        concl = run.get('conclusion')
        run_link = f"https://github.com/{OWNER}/{repo}/actions/runs/{run['id']}"
        print(f"🔍 {title} | {branch} | {ts} | Статус: {concl} | {run_link}")

        # Формируем префикс для txt файлов
        run_prefix = f"run_{i + 1:02d}_{sha[:7]}"
        save_dir = logs_dir if SAVE_LOGS else None

        # Готовим информацию для кэширования
        run_info = {
            'branch': branch,
            'title': title,
            'conclusion': concl,
            'timestamp': ts,
            'sha': sha
        }

        zbytes = download_logs_bytes(OWNER, repo, run['id'], save_dir, run_prefix, run_info)
        if zbytes:
            test_details = parse_failed_tests_with_details(zbytes)
            failed = set(test_details.keys())
            all_test_details.update(test_details)
        else:
            failed = set()

        summary[sha] = failed
        meta[sha] = {'title': title, 'ts': ts, 'concl': concl, 'link': run_link, 'branch': branch}

    # Выводим информацию о сохранённых логах
    if SAVE_LOGS:
        saved_logs = list(logs_dir.glob("*.txt"))
        if saved_logs:
            print(f"\n💾 Всего сохранено {len(saved_logs)} txt файлов в {logs_dir}")
        else:
            print(f"\n⚠ txt файлы не были сохранены в {logs_dir}")

    # Добавляем все детали тестов в HTML builder
    html_builder.add_test_details(all_test_details)

    # Проверяем, что summary не пустой
    if not summary:
        print("❌ Нет данных для анализа.")
        return

    # --- 4. Выводим информацию о самом раннем run (ПОЛНЫЙ СПИСОК В КОНСОЛЬ) --- #
    first_sha = next(iter(summary))
    first_fail = summary[first_sha]
    first_meta = meta[first_sha]
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
                                 for t in first_fail],
                             commit_info=first_meta)

    # --- 5. Дифф по запускам (ПОЛНЫЙ СПИСОК В КОНСОЛЬ) --- #
    print("\n=== 📊 Изменения падений тестов по последним запускам ===")
    prev = set()
    for sha, curr in summary.items():
        added = curr - prev
        removed = prev - curr
        info = meta[sha]
        print(f"\n📦 {info['title']} | {info['ts']} | {info['concl']} | {info['link']}")

        # Начинаем новую секцию run'а в HTML
        html_builder.start_run_section(info)

        print(f"➕ Новые падения ({len(added)} шт.):" if added else "➕ Новые падения: нет")
        if added:
            for t in sorted(added):
                marker = "" if BRANCH == MASTER_BRANCH else \
                    (" (также в master)" if t in master_failed else " (только здесь)")
                print(f"    {t}{marker}")
        html_builder.add_run_section("➕ Новые падения",
                                     [
                                         f"{t}{'' if BRANCH == MASTER_BRANCH else ' (также в master)' if t in master_failed else ' (только здесь)'}"
                                         for t in added])

        print(f"✔ Починились ({len(removed)} шт.):" if removed else "✔ Починились: нет")
        if removed:
            for t in sorted(removed):
                print(f"    {t}")
        html_builder.add_run_section("✔ Починились", removed)

        only_here = curr - master_failed if BRANCH != MASTER_BRANCH else set()
        print(f"⚠ Уникальные падения ({len(only_here)} шт.):" if only_here else "⚠ Уникальные падения: нет")
        if only_here:
            for t in sorted(only_here):
                print(f"    {t}")
        html_builder.add_run_section("⚠ Уникальные падения", only_here)

        prev = curr

    # --- 6. Генерируем HTML в конце --- #
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

    # Показываем финальную статистику кэша
    final_stats = artifact_cache.get_cache_stats()
    print(
        f"\n📊 Финальная статистика кэша: {final_stats['total_cached']} артефактов ({final_stats['total_size_mb']} МБ)")


if __name__ == '__main__':
    main()
