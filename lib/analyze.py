#!/usr/bin/env python3
import re
import requests
import zipfile
import io
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

        # Инициализируем внутренний кэш артефактов
        if cache_dir is None:
            cache_dir = Path('downloaded_logs') / 'cache'
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_metadata_file = cache_dir / 'metadata.json'
        self.artifact_cache = ArtifactCache(cache_dir, cache_metadata_file)

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

                    # Сначала пробуем прочитать sidecar рядом с zip через ArtifactCache
                    cached = self.artifact_cache.load_parsed_sidecar(self.owner, repo, run['id'])
                    if cached is not None:
                        details, has_no_tests = cached
                    else:
                        # Скачиваем и парсим логи один раз
                        zbytes = self.download_logs(repo, run['id'])
                        if not zbytes:
                            print(f"⚠ Не удалось скачать логи для run {run['id']}, пропускаем")
                            continue
                        details, has_no_tests = self.parse_details_and_flags(zbytes)
                        # Сохраняем sidecar для будущего переиспользования
                        self.artifact_cache.save_parsed_sidecar(self.owner, repo, run['id'], details, has_no_tests)
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
        # 1) Пробуем прочитать из кэша
        if self.artifact_cache and self.artifact_cache.has_cached(self.owner, repo, run_id):
            zip_bytes = self.artifact_cache.get_cached(self.owner, repo, run_id)
            if zip_bytes is None:
                print(f"⚠ Ошибка чтения кэшированного артефакта для run {run_id}")
            else:
                # Сохраняем txt (если включено)
                effective_dir = save_dir or (self.log_save_dir if self.save_logs else None)
                if effective_dir is not None and zip_bytes:
                    saved = self.artifact_cache.save_txt_from_zip(zip_bytes, effective_dir, run_prefix)
                    if saved:
                        print(f"💾 Сохранено {saved} txt файлов в {effective_dir}")
                return zip_bytes

        # 2) Иначе скачиваем из API
        url = f'https://api.github.com/repos/{self.owner}/{repo}/actions/runs/{run_id}/logs'
        try:
            response = self.github_get(url)
            zip_bytes = response.content
            if zip_bytes:
                print(f"⬇️ Скачиваем новый артефакт для run {run_id}")
                # Сохраняем в кэш
                if self.artifact_cache:
                    stored = self.artifact_cache.store_artifact(self.owner, repo, run_id, zip_bytes, run_info)
                    if stored:
                        print(f"💾 Артефакт сохранён в кэш для run {run_id}")
                # Сохраняем txt (если включено)
                effective_dir = save_dir or (self.log_save_dir if self.save_logs else None)
                if effective_dir is not None:
                    saved = self.artifact_cache.save_txt_from_zip(zip_bytes, effective_dir, run_prefix)
                    if saved:
                        print(f"💾 Сохранено {saved} txt файлов в {effective_dir}")
            return zip_bytes
        except requests.RequestException as e:
            print(f"⚠ Не могу скачать логи run {run_id}: {e}")
            return None

    def parse_failed_tests_with_details(self, zip_bytes: bytes) -> Dict[str, List[Dict]]:
        """
        Парсит логи согласно алгоритму:
        1. Находит секцию ##[group]🚀 Publish results
        2. Читает статистику тестов (следующая строка)
        3. Если failed > 0, читает упавшие тесты (🧪)
        4. Связывает их с описанием ошибок (##[error])
        5. Завершает на ##[endgroup]
        """
        failed = {}  # test_name -> list of details

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for name in z.namelist():
                if not name.lower().endswith('.txt'):
                    continue

                with z.open(name) as f:
                    lines = [line.decode('utf-8', errors='ignore').rstrip() for line in f]

                i = 0
                while i < len(lines):
                    line = lines[i]

                    # 1. Ищем начало секции Publish results
                    if self.pattern_publish_group.search(line):
                        # print(f"🚀 Найдена секция Publish results в файле {name}")

                        # 2. Следующая строка должна быть статистикой тестов
                        i += 1
                        if i >= len(lines):
                            break

                        stat_line = lines[i]
                        stats_match = self.pattern_test_results.search(stat_line)
                        if not stats_match:
                            continue

                        project_name = stats_match.group(1)
                        total_tests = int(stats_match.group(2))
                        passed_tests = int(stats_match.group(3))
                        skipped_tests = int(stats_match.group(4))
                        failed_count = int(stats_match.group(5))

                        print(f"📊 Статистика {project_name}: {total_tests} всего, {failed_count} провалено")

                        # Если нет падений, пропускаем до конца группы
                        if failed_count == 0:
                            while i < len(lines) and not self.pattern_end_group.search(lines[i]):
                                i += 1
                            continue

                        # 3. Собираем упавшие тесты (строки с 🧪)
                        failed_tests = {}  # test_name -> {'description': str, 'details': str}
                        i += 1

                        while i < len(lines):
                            test_line = lines[i]
                            test_match = self.pattern_test_line.match(test_line)
                            if not test_match:
                                break

                            test_key = test_match.group(1).strip()
                            description = test_match.group(2).strip() if test_match.group(2) else ''

                            test_name = '.'.join((test_key, description))
                            failed_tests[test_name] = {'description': description, 'details': ''}
                            # print(f"🧪 Найден упавший тест: {test_name}")
                            i += 1

                        # 4. Собираем секции ошибок (##[error])
                        while i < len(lines):
                            error_line = lines[i]

                            # Конец группы - завершаем парсинг этой секции
                            if self.pattern_end_group.search(error_line):
                                break

                            error_match = self.pattern_error_line.search(error_line)
                            if error_match:
                                error_description = error_match.group(1).strip()

                                # Собираем детали ошибки до следующего ##[error] или ##[endgroup]
                                details_lines = []
                                i += 1
                                while i < len(lines):
                                    next_line = lines[i]
                                    if (self.pattern_error_line.search(next_line) or
                                            self.pattern_end_group.search(next_line)):
                                        break
                                    details_lines.append(next_line)
                                    i += 1

                                details_text = '\n'.join(details_lines).strip()

                                # Связываем ошибку с тестом по названию
                                matched_test = self._match_error_to_test_by_description(
                                    error_description, failed_tests)
                                if matched_test:
                                    failed_tests[matched_test]['details'] += (
                                        f"\n{error_description}\n{details_text}\n---\n")
                                    # print(f"🔗 Связал ошибку '{error_description}' с тестом '{matched_test}'")

                                continue

                            i += 1

                        # Добавляем результаты в общий словарь
                        for test_name, test_data in failed_tests.items():
                            if test_data['details'].strip():
                                if test_name not in failed:
                                    failed[test_name] = []
                                failed[test_name].append({
                                    'file': name,
                                    'line_num': 0,
                                    'context': test_data['details'].strip(),
                                    'project': project_name
                                })
                    else:
                        i += 1

        return failed

    def parse_details_and_flags(self, zip_bytes: bytes) -> Tuple[Dict[str, List[Dict]], bool]:
        """Парсит zip один раз и возвращает (failed_details, has_no_tests_error).

        - failed_details: как в parse_failed_tests_with_details()
        - has_no_tests_error: True, если в логах встречено сообщение об отсутствии результатов тестов
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
                for ln in lines:
                    if self.pattern_no_tests.search(ln):
                        has_no_tests = True
                        break
                if has_no_tests:
                    break

                i = 0
                while i < len(lines):
                    line = lines[i]
                    if self.pattern_publish_group.search(line):
                        i += 1
                        if i >= len(lines):
                            break
                        stat_line = lines[i]
                        stats_match = self.pattern_test_results.search(stat_line)
                        if not stats_match:
                            continue
                        project_name = stats_match.group(1)
                        failed_count = int(stats_match.group(5))
                        if failed_count == 0:
                            # пропускаем до конца группы
                            while i < len(lines) and not self.pattern_end_group.search(lines[i]):
                                i += 1
                            continue

                        failed_tests: Dict[str, Dict[str, str]] = {}
                        i += 1
                        while i < len(lines):
                            test_line = lines[i]
                            test_match = self.pattern_test_line.match(test_line)
                            if not test_match:
                                break
                            test_key = test_match.group(1).strip()
                            description = test_match.group(2).strip() if test_match.group(2) else ''
                            test_name = '.'.join((test_key, description))
                            failed_tests[test_name] = {'description': description, 'details': ''}
                            i += 1

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
                                matched_test = self._match_error_to_test_by_description(
                                    error_description, failed_tests)
                                if matched_test:
                                    failed_tests[matched_test]['details'] += (
                                        f"\n{error_description}\n{details_text}\n---\n")
                                continue
                            i += 1

                        for tname, tdata in failed_tests.items():
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

    def parse_failed_tests(self, zip_bytes: bytes) -> Set[str]:
        """Возвращает только set путей для обратной совместимости."""
        details = self.parse_failed_tests_with_details(zip_bytes)
        return set(details.keys())

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
                # Сначала пробуем sidecar через ArtifactCache
                cached = self.artifact_cache.load_parsed_sidecar(self.owner, repo, run['id'])
                if cached is not None:
                    test_details, has_no_tests = cached
                    if has_no_tests:
                        print("⚠ Пропускаем run: нет результатов тестов")
                        test_details = {}
                else:
                    # Фоллбек: один раз парсим, сохраняем sidecar
                    zbytes = self.download_logs(
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
                    if not zbytes:
                        test_details = {}
                    else:
                        test_details, has_no_tests = self.parse_details_and_flags(zbytes)
                        self.artifact_cache.save_parsed_sidecar(self.owner, repo, run['id'], test_details, has_no_tests)
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

        zbytes = self.download_logs(repo, master_run['id'])
        if not zbytes:
            return set()

        return self.parse_failed_tests(zbytes)

    def _match_error_to_test_by_description(self, error_description: str, failed_tests: Dict) -> Optional[str]:
        """Находит подходящий тест для ошибки по description."""
        error_desc_norm = self._normalize_test_name(error_description)

        for test_name, test_data in failed_tests.items():
            test_desc_norm = self._normalize_test_name(test_data['description'])
            if error_desc_norm == test_desc_norm:
                return test_name

        return None

    def _normalize_test_name(self, name: str) -> str:
        """Нормализует название теста для сравнения."""
        return name.lower().strip()


class TestAnalysisResults:
    """Класс для хранения и обработки результатов анализа тестов."""

    def __init__(self, repo: str, branch: str):
        self.repo = repo
        self.branch = branch
        self.summary = {}  # sha -> set(failed_tests)
        self.meta = {}  # sha -> run metadata
        self.test_details = {}  # test_name -> details
        self.master_failed = set()
        print(f"📊 Инициализирован анализ результатов для {repo}/{branch}")

    def add_run_data(self, summary: Dict, meta: Dict, test_details: Dict):
        """Добавляет данные анализа запусков."""
        self.summary.update(summary)
        self.meta.update(meta)
        self.test_details.update(test_details)
        print(f"📈 Добавлены данные по {len(summary)} запускам")

    def set_master_failed(self, master_failed: Set[str]):
        """Устанавливает список падающих тестов в master."""
        self.master_failed = master_failed
        print(f"🔧 Установлен список падающих тестов в master: {len(master_failed)} тестов")

    def analyze_test_behavior(self) -> Dict[str, Any]:
        """
        Анализирует поведение тестов во всех ранах.

        Returns:
            Dict с ключами:
            - stable_failing: Dict[str, Dict] - тесты, которые стабильно падают
            - fixed_tests: Dict[str, Dict] - тесты, которые починились
            - flaky_tests: Dict[str, Dict] - тесты, которые то падают, то проходят
        """
        if not self.summary:
            return {'stable_failing': {}, 'fixed_tests': {}, 'flaky_tests': {}}

        # Собираем все уникальные тесты
        all_tests = set()
        for failed_set in self.summary.values():
            all_tests.update(failed_set)

        print(f"🔍 Анализируем поведение {len(all_tests)} уникальных тестов в {len(self.summary)} ранах")

        # Создаем матрицу состояний для каждого теста в каждом ране
        ordered_shas = list(self.summary.keys())  # Раны уже в хронологическом порядке
        test_states = {}  # test_name -> [True/False] для каждого рана

        for test in all_tests:
            states = []
            for sha in ordered_shas:
                states.append(test in self.summary[sha])
            test_states[test] = states

        # Анализируем паттерны
        stable_failing = {}
        fixed_tests = {}
        flaky_tests = {}

        for test, states in test_states.items():
            behavior = self._analyze_test_pattern(test, states, ordered_shas)

            if behavior['type'] == 'stable_failing':
                stable_failing[test] = behavior
            elif behavior['type'] == 'fixed':
                fixed_tests[test] = behavior
            elif behavior['type'] == 'flaky':
                flaky_tests[test] = behavior

        # Логируем результаты
        print(f"📊 Результаты анализа:")
        print(f"  🔴 Стабильно падающие: {len(stable_failing)} тестов")
        print(f"  ✅ Починенные: {len(fixed_tests)} тестов")
        print(f"  🟡 Нестабильные (flaky): {len(flaky_tests)} тестов")

        return {
            'stable_failing': stable_failing,
            'fixed_tests': fixed_tests,
            'flaky_tests': flaky_tests
        }

    def _analyze_test_pattern(self, test_name: str, states: List[bool], shas: List[str]) -> Dict[str, Any]:
        """Анализирует паттерн поведения одного теста."""
        first_fail_idx = None
        last_fail_idx = None
        fail_count = 0

        # Находим первое и последнее падение, считаем общее количество падений
        for i, is_failed in enumerate(states):
            if is_failed:
                if first_fail_idx is None:
                    first_fail_idx = i
                last_fail_idx = i
                fail_count += 1

        if fail_count == 0:
            # Тест никогда не падал (не должно происходить, так как мы берем только падавшие)
            return {'type': 'never_failed', 'details': {}}

        total_runs = len(states)

        # Определяем тип поведения
        if fail_count == 1:
            # Упал только один раз
            behavior_type = 'single_failure'
        elif first_fail_idx == last_fail_idx:
            # Упал только в одном ране (не должно происходить при fail_count > 1)
            behavior_type = 'single_failure'
        elif last_fail_idx == total_runs - 1:
            # Последнее падение в последнем ране
            if self._is_stable_failing_from(states, first_fail_idx):
                behavior_type = 'stable_failing'
            else:
                behavior_type = 'flaky'
        else:
            # Последнее падение не в последнем ране - значит, тест починился
            if self._has_flaky_behavior(states):
                behavior_type = 'flaky'
            else:
                behavior_type = 'fixed'

        # Собираем детальную информацию
        failed_runs = []
        for i, is_failed in enumerate(states):
            if is_failed:
                failed_runs.append({
                    'sha': shas[i],
                    'meta': self.meta[shas[i]],
                    'run_number': i + 1
                })

        # Ищем ссылку на PR/коммит после последнего упавшего run
        next_pr_link = None
        next_commit_info = None
        if last_fail_idx is not None and last_fail_idx + 1 < total_runs:
            next_run_sha = shas[last_fail_idx + 1]
            next_run_meta = self.meta.get(next_run_sha)
            if next_run_meta:
                # Берем ссылку на run (который содержит информацию о коммите)
                next_pr_link = next_run_meta.get('link')
                next_commit_info = {
                    'sha': next_run_sha[:7],
                    'title': next_run_meta.get('title', ''),
                    'ts': next_run_meta.get('ts', ''),
                    'link': next_pr_link
                }

        return {
            'type': behavior_type,
            'test_name': test_name,
            'total_runs': total_runs,
            'fail_count': fail_count,
            'first_fail_run': first_fail_idx + 1 if first_fail_idx is not None else None,
            'last_fail_run': last_fail_idx + 1 if last_fail_idx is not None else None,
            'failed_runs': failed_runs,
            'pattern': ''.join(['F' if s else 'P' for s in states]),  # F=Failed, P=Passed
            'details': self.test_details.get(test_name, []),
            'next_pr_link': next_pr_link,
            'next_commit_info': next_commit_info
        }

    def _is_stable_failing_from(self, states: List[bool], start_idx: int) -> bool:
        """Проверяет, стабильно ли падает тест начиная с указанного индекса."""
        if start_idx >= len(states):
            return False

        # Проверяем, что с момента первого падения тест падает во всех последующих ранах
        for i in range(start_idx, len(states)):
            if not states[i]:
                return False
        return True

    def _has_flaky_behavior(self, states: List[bool]) -> bool:
        """Проверяет, есть ли у теста нестабильное поведение (чередование падений и успехов)."""
        if len(states) < 2:
            return False

        # Считаем количество переходов между состояниями
        transitions = 0
        for i in range(1, len(states)):
            if states[i] != states[i - 1]:
                transitions += 1

        # Если больше одного перехода, считаем тест нестабильным
        return transitions > 2

    def get_run_diffs(self) -> List[Dict]:
        """Возвращает список изменений между запусками."""
        diffs = []
        prev = set()

        for sha, curr in self.summary.items():
            added = curr - prev
            removed = prev - curr
            only_here = curr - self.master_failed if self.master_failed else set()

            print(f"📊 Диф для {sha[:7]}: +{len(added)} -{len(removed)} (уникальных: {len(only_here)})")

            diffs.append({
                'sha': sha,
                'meta': self.meta[sha],
                'added': added,
                'removed': removed,
                'only_here': only_here,
                'current': curr
            })

            prev = curr

        return diffs

    def get_statistics(self) -> Dict:
        """Возвращает общую статистику анализа."""
        if not self.summary:
            return {}

        all_failed = set()
        for failed_set in self.summary.values():
            all_failed.update(failed_set)

        stats = {
            'total_runs': len(self.summary),
            'unique_failed_tests': len(all_failed),
            'master_failed_tests': len(self.master_failed),
            'new_failures': len(all_failed - self.master_failed) if self.master_failed else 0
        }

        print(f"📊 Общая статистика: {stats['total_runs']} запусков, {stats['unique_failed_tests']} уникальных падений")

        return stats
