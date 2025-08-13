import re
import requests
import zipfile
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, List, Optional, Tuple, Any
from .cache import ArtifactCache


class GitHubWorkflowAnalyzer:
    """Анализатор GitHub Actions workflows для отслеживания падающих тестов."""

    def __init__(self, github_token: str, owner: str, workflow_file: str = 'ci.yml', cache_dir: Optional[Path] = None):
        self.github_token = github_token
        self.owner = owner
        self.workflow_file = workflow_file

        # Инициализируем хранилище распарсенных данных в MongoDB
        # Параметр cache_dir сохранён для обратной совместимости, но больше не используется.
        mongo_uri = os.getenv('MONGO_URI', 'mongodb://root:example@localhost:27017')
        self.artifact_cache = ArtifactCache(mongo_uri=mongo_uri)

        # Настройка HTTP заголовков
        self.headers = {
            'Authorization': f'token {github_token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        # Регулярные выражения для парсинга логов
        self.pattern_publish_group = re.compile(r"##\[group\]🚀 Publish results")
        self.pattern_test_results = re.compile(
            r"ℹ️ - test results (.*?) - (\d+) tests run, (\d+) passed, (\d+) skipped, (\d+) failed")
        self.pattern_test_line = re.compile(r".*?🧪 - (.*?)(?: \| (.*))?$")
        self.pattern_error_line = re.compile(r"##\[error\](.*)$")
        self.pattern_end_group = re.compile(r"##\[endgroup\]")
        # Ошибка отсутствия результатов тестов
        self.pattern_no_tests = re.compile(r"No test results found", re.IGNORECASE)

        # Настройки сохранения txt логов
        self.save_logs = False
        self.log_save_dir: Optional[Path] = None

    def configure_cache(self, save_logs: bool = False, log_save_dir: Optional[Path] = None):
        """Конфигурирует сохранение txt-логов (артефакт-кэш задаётся через конструктор)."""
        self.save_logs = bool(save_logs)
        self.log_save_dir = log_save_dir

    # --- JSON sidecar теперь обрабатывается в ArtifactCache --- #

    def github_get(self, url: str, **kwargs) -> requests.Response:
        """Выполняет GET запрос к GitHub API с обработкой ошибок."""
        response = requests.get(url, headers=self.headers, **kwargs)
        response.raise_for_status()
        return response

    # --- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ДЛЯ УМЕНЬШЕНИЯ ДУБЛИРОВАНИЯ --- #

    def _effective_save_dir(self, save_dir: Optional[Path]) -> Optional[Path]:
        """Возвращает итоговую директорию сохранения txt логов с учётом глобальной настройки."""
        return save_dir or (self.log_save_dir if self.save_logs else None)

    def _maybe_save_txt(self, zip_bytes: Optional[bytes], save_dir: Optional[Path], run_prefix: str) -> None:
        """При наличии байтов zip и директории пытается извлечь и сохранить txt логи."""
        effective_dir = self._effective_save_dir(save_dir)
        if effective_dir is not None and zip_bytes:
            saved = self.artifact_cache.save_txt_from_zip(zip_bytes, effective_dir, run_prefix)
            if saved:
                print(f"💾 Сохранено {saved} txt файлов в {effective_dir}")

    def _load_or_parse_run_details(self, repo: str, run_id: int,
                                   run_info: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, List[Dict]], bool]:
        """Пытается загрузить распарсенные детали тестов из sidecar или скачать и распарсить логи.

        Возвращает кортеж (details, has_no_tests), где:
        - details: Dict[test_name, List[detail]]
        - has_no_tests: True, если в логах было сообщение об отсутствии результатов тестов
        """
        # 1) Пробуем загрузить распарсенные результаты из Mongo через ArtifactCache
        cached = self.artifact_cache.load_parsed_sidecar(self.owner, repo, run_id)
        if cached is not None:
            details, has_no_tests = cached
            return details or {}, has_no_tests

        # 2) Иначе скачиваем и парсим
        zbytes = self.download_logs(repo, run_id, run_info=run_info)
        if not zbytes:
            print(f"⚠ Не удалось получить логи для run {run_id}")
            return {}, False

        details, has_no_tests = self.parse_details_and_flags(zbytes)
        # Сохраняем sidecar для будущего переиспользования
        self.artifact_cache.save_parsed_sidecar(self.owner, repo, run_id, details, has_no_tests)
        return details or {}, has_no_tests

    # --- Общая логика парсинга zip логов тестов --- #

    def _parse_zip_internal(self, zip_bytes: bytes, *, detect_no_tests: bool, test_name_joiner: str
                             ) -> Tuple[Dict[str, List[Dict]], bool]:
        """Единый парсер zip логов.

        Args:
            zip_bytes: содержимое zip с txt логами.
            detect_no_tests: искать ли флаг отсутствия результатов тестов и завершать ранний проход.
            test_name_joiner: разделитель между ключом теста и описанием (например, ' | ' или '.').

        Returns:
            (failed_details, has_no_tests)
        """
        failed: Dict[str, List[Dict]] = {}
        has_no_tests = False

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for name in z.namelist():
                if not name.lower().endswith('.txt'):
                    continue

                with z.open(name) as f:
                    lines = [line.decode('utf-8', errors='ignore').rstrip() for line in f]

                # Быстрый проход на наличие "No test results"
                if detect_no_tests:
                    for ln in lines:
                        if self.pattern_no_tests.search(ln):
                            has_no_tests = True
                            break
                    if has_no_tests:
                        break

                i = 0
                while i < len(lines):
                    line = lines[i]
                    # Ищем начало секции Publish results
                    if self.pattern_publish_group.search(line):
                        # Следующая строка должна быть статистикой тестов
                        i += 1
                        if i >= len(lines):
                            break
                        stat_line = lines[i]
                        stats_match = self.pattern_test_results.search(stat_line)
                        if not stats_match:
                            continue

                        project_name = stats_match.group(1)
                        failed_count = int(stats_match.group(5))

                        # Если нет падений, пропускаем до конца группы
                        if failed_count == 0:
                            while i < len(lines) and not self.pattern_end_group.search(lines[i]):
                                i += 1
                            continue

                        # Собираем упавшие тесты (строки с 🧪)
                        failed_tests: List[Tuple[str, Dict[str, str]]] = []
                        i += 1
                        while i < len(lines):
                            test_line = lines[i]
                            test_match = self.pattern_test_line.match(test_line)
                            if not test_match:
                                break

                            test_key = test_match.group(1).strip()
                            description = test_match.group(2).strip() if test_match.group(2) else ''

                            # Воссоздаём прежнее поведение: всегда join из двух частей, даже если description пустой
                            test_name = test_name_joiner.join((test_key, description))
                            failed_tests.append((test_name, {'description': description, 'details': ''}))
                            i += 1

                        # Собираем секции ошибок (##[error])
                        errors = []
                        while i < len(lines):
                            error_line = lines[i]
                            if self.pattern_end_group.search(error_line):
                                break
                            error_match = self.pattern_error_line.search(error_line)
                            if error_match:
                                error_description = error_match.group(1).strip()
                                details_lines: List[str] = []
                                i += 1
                                while i < len(lines):
                                    next_line = lines[i]
                                    if (self.pattern_error_line.search(next_line) or
                                            self.pattern_end_group.search(next_line)):
                                        break
                                    details_lines.append(next_line)
                                    i += 1
                                details_text = '\n'.join(details_lines).strip()
                                errors.append(f"\n{error_description}\n{details_text}\n---\n")
                                continue
                            i += 1

                        # Добавляем результаты
                        res = {}
                        for index_error in range(len(failed_tests)):
                            res[failed_tests[index_error][0]] = {**failed_tests[index_error][1],
                                                                 **{'details': errors[index_error]}}

                        for tname, tdata in res.items():
                            if tdata['details'].strip():
                                if tname not in failed:
                                    failed[tname] = []
                                failed[tname].append({
                                    'file': name,
                                    'line_num': 0,
                                    'context': tdata['details'].strip(),
                                    'project': project_name
                                })
                    else:
                        i += 1

        return failed, has_no_tests

    def get_recent_runs(self, repo: str, branch: str, max_runs: int) -> List[Dict]:
        """
        Возвращает max_runs завершённых (success|failure) workflow-ранов с ВАЛИДНЫМИ результатами тестов,
        отсортированных от нового к старому (в порядке обработки). Для каждого run выполняется парсинг логов
        единожды и распарсенные данные прокидываются дальше в поле 'parsed_test_details'.
        """
        collected = []
        page = 1
        per_page = 100

        print(f"🔍 Ищем до {max_runs} валидных runs для {repo}/{branch}")

        while len(collected) < max_runs:
            url = f'https://api.github.com/repos/{self.owner}/{repo}/actions/workflows/{self.workflow_file}/runs'
            params = {'branch': branch, 'per_page': per_page, 'page': page}

            try:
                resp = self.github_get(url, params=params)
                items = resp.json().get('workflow_runs', [])
                if not items:
                    print(f"📄 Страница {page} пуста, завершаем поиск")
                    break

                for run in items:
                    # Проверяем базовые условия
                    if run['status'] != 'completed' or run.get('conclusion') not in ('success', 'failure'):
                        continue

                    print(f"🔍 Проверяем run {run['id']} ({run.get('conclusion')})")

                    # Загружаем или парсим детали единожды через хелпер
                    details, has_no_tests = self._load_or_parse_run_details(repo, run['id'])
                    if has_no_tests:
                        print(f"⚠ Run {run['id']} не содержит результатов тестов (No test results), пропускаем")
                        continue
                    if not details:
                        print(f"⚠ Run {run['id']} не содержит валидных результатов тестов, пропускаем")
                        continue
                    # Сохраняем распарсенные детали в объект run, чтобы не парсить повторно позже
                    run['parsed_test_details'] = details

                    # Если дошли сюда - run валидный
                    print(f"✅ Run {run['id']} валидный, добавляем в результат")
                    collected.append(run)

                    if len(collected) == max_runs:
                        print(f"🎯 Собрали нужное количество runs: {max_runs}")
                        break

                page += 1

            except requests.RequestException as e:
                print(f"⚠ Ошибка получения runs для {repo}: {e}")
                break
        collected.reverse()
        print(f"📊 Найдено {len(collected)} валидных runs")
        return collected

    def get_latest_completed_run(self, repo: str, branch: str) -> Optional[Dict]:
        """Возвращает последний COMPLETED run (success|failure)."""
        url = f'https://api.github.com/repos/{self.owner}/{repo}/actions/workflows/{self.workflow_file}/runs'
        params = {'branch': branch, 'per_page': 50}

        try:
            response = self.github_get(url, params=params)
            for run in response.json().get('workflow_runs', []):
                if run['status'] == 'completed' and run.get('conclusion') in ('success', 'failure'):
                    return run
        except requests.RequestException as e:
            print(f"⚠ Ошибка получения последнего run для {repo}/{branch}: {e}")

        return None

    def get_commit_title(self, repo: str, sha: str) -> str:
        """Получает заголовок коммита по SHA."""
        url = f'https://api.github.com/repos/{self.owner}/{repo}/commits/{sha}'
        try:
            response = self.github_get(url)
            return response.json().get('commit', {}).get('message', '').splitlines()[0]
        except requests.RequestException:
            return sha[:7]  # Возвращаем сокращённый SHA в случае ошибки

    def download_logs(self, repo: str, run_id: int, *, save_dir: Optional[Path] = None,
                      run_prefix: str = "", run_info: Optional[Dict[str, Any]] = None) -> Optional[bytes]:
        """Скачивает логи workflow run'а с использованием кэша и опциональным сохранением txt."""
        # Скачиваем из API (zip не кэшируем на диск)
        url = f'https://api.github.com/repos/{self.owner}/{repo}/actions/runs/{run_id}/logs'
        try:
            response = self.github_get(url)
            zip_bytes = response.content
            if zip_bytes:
                print(f"⬇️ Скачиваем новый артефакт для run {run_id}")
                # Сохраняем txt (если включено)
                self._maybe_save_txt(zip_bytes, save_dir, run_prefix)
            return zip_bytes
        except requests.RequestException as e:
            print(f"⚠ Не могу скачать логи run {run_id}: {e}")
            return None

    def parse_details_and_flags(self, zip_bytes: bytes) -> Tuple[Dict[str, List[Dict]], bool]:
        """
        Парсит zip один раз и возвращает (failed_details, has_no_tests_error).
        - failed_details: как в parse_failed_tests_with_details()
        - has_no_tests_error: True, если в логах встречено сообщение об отсутствии результатов тестов
        """
        return self._parse_zip_internal(zip_bytes, detect_no_tests=True, test_name_joiner=' | ')

    def analyze_repo_runs(self, repo: str, branch: str, max_runs: int) -> Tuple[Dict, Dict, Dict]:
        """
        Анализирует последние запуски репозитория.

        Returns:
            Tuple[Dict, Dict, Dict]: (summary, meta, all_test_details)
            - summary: sha -> set(failed_tests)
            - meta: sha -> {'title', 'ts', 'concl', 'link', 'branch'}
            - all_test_details: test_name -> list of details
        """
        runs = self.get_recent_runs(repo, branch, max_runs)
        if not runs:
            return {}, {}, {}

        summary, meta, all_test_details = {}, {}, {}

        for i, run in enumerate(runs):
            sha = run['head_sha']
            title = self.get_commit_title(repo, sha) or sha[:7]
            branch_name = run.get('head_branch')
            ts = datetime.fromisoformat(
                (run.get('run_started_at') or run.get('created_at')).replace('Z', '+00:00')
            ).strftime('%Y-%m-%d %H:%M:%S')
            concl = run.get('conclusion')
            run_link = f"https://github.com/{self.owner}/{repo}/actions/runs/{run['id']}"

            print(f"🔍 {title} | {branch_name} | {ts} | Статус: {concl} | {run_link}")

            # Используем предварительно распарсенные детали, если они есть
            test_details = run.get('parsed_test_details')
            if test_details is None:
                # Сначала пробуем получить через единый хелпер
                test_details, has_no_tests = self._load_or_parse_run_details(
                    repo,
                    run['id'],
                    run_info={
                        'title': title,
                        'ts': ts,
                        'concl': concl,
                        'link': run_link,
                        'branch': branch_name
                    }
                )
                if has_no_tests:
                    print("⚠ Пропускаем run: нет результатов тестов")
                    test_details = {}

            failed = set(test_details.keys()) if test_details else set()
            if test_details:
                all_test_details.update(test_details)

            summary[sha] = failed
            meta[sha] = {
                'title': title,
                'ts': ts,
                'concl': concl,
                'link': run_link,
                'branch': branch_name
            }

        return summary, meta, all_test_details

    def get_master_failed_tests(self, repo: str, master_branch: str = 'master') -> Set[str]:
        """Получает список падающих тестов в master ветке."""
        master_run = self.get_latest_completed_run(repo, master_branch)
        if not master_run:
            return set()
        # 1) Сначала пробуем загрузить распарсенные данные из Mongo
        cached = self.artifact_cache.load_parsed_sidecar(self.owner, repo, master_run['id'])
        if cached is not None:
            details, has_no_tests = cached
            if has_no_tests or not details:
                return set()
            return set(details.keys())
        # 2) Если нет — скачиваем и парсим, затем сохраняем в Mongo
        zbytes = self.download_logs(repo, master_run['id'])
        if not zbytes:
            return set()
        details, _ = self._parse_zip_internal(zbytes, detect_no_tests=False, test_name_joiner=' | ')
        # Сохраняем в Mongo для будущего использования
        try:
            self.artifact_cache.save_parsed_sidecar(self.owner, repo, master_run['id'], details, has_no_tests=False)
        except Exception:
            pass
        return set(details.keys())
