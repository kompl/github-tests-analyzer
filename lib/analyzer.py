import re
import requests
import zipfile
import io
import xml.etree.ElementTree as ET
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, List, Optional, Tuple, Any, Callable
from .cache import ArtifactCache


class LogTestResultsExtractor:
    """Экстрактор результатов тестов из zip-логов GitHub Actions (основной способ).

    Выполняет парсинг текстовых логов, извлечённых из zip, и строит структуру
    упавших тестов с деталями. Также умеет скачивать zip логов через переданную
    функцию скачивания.
    """

    def __init__(self, download_logs_func):
        self.download_logs_func = download_logs_func

        # Регулярные выражения для парсинга логов
        self.pattern_publish_group = re.compile(r"##\[group\]🚀 Publish results")
        self.pattern_test_results = re.compile(
            r"ℹ️ - test results (.*?) - (\d+) tests run, (\d+) passed, (\d+) skipped, (\d+) failed")
        self.pattern_test_line = re.compile(r".*?🧪 - (.*?)(?: \| (.*))?$")
        self.pattern_error_line = re.compile(r"##\[error\](.*)$")
        self.pattern_end_group = re.compile(r"##\[endgroup\]")
        # Ошибка отсутствия результатов тестов
        self.pattern_no_tests = re.compile(r"No test results found", re.IGNORECASE)

    def parse_zip(self, zip_bytes: bytes, *, detect_no_tests: bool = True, test_name_joiner: str = ' | '
                  ) -> Tuple[Dict[str, List[Dict]], bool]:
        """Парсит zip логов и возвращает (failed_details, has_no_tests)."""
        failed: Dict[str, List[Dict]] = {}
        has_no_tests = False
        # Глобальный счётчик порядка для всего zip логов
        order_pos = 0

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
                        failed_tests: List[Tuple[str, Dict[str, Any]]] = []
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
                            failed_tests.append((test_name, {'description': description, 'details': '', 'order_index': order_pos}))
                            order_pos += 1
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
                                    'project': project_name,
                                    'order_index': tdata.get('order_index')
                                })
                    else:
                        i += 1

        return failed, has_no_tests

    def extract(self, repo: str, run_id: int, run_info: Optional[Dict[str, Any]] = None
                ) -> Tuple[Dict[str, List[Dict]], bool]:
        """Скачивает и парсит zip логов для указанного run."""
        zip_bytes = self.download_logs_func(repo, run_id, run_info=run_info)
        if not zip_bytes:
            print(f"⚠ Не удалось получить zip логов для run {run_id}")
            return {}, False
        return self.parse_zip(zip_bytes, detect_no_tests=True, test_name_joiner=' | ')


class ArtifactsTestResultsExtractor:
    """Экстрактор результатов тестов из артефактов GitHub (JUnit XML).

    Логика:
    1) Запрашивает список артефактов ранa: /repos/{owner}/{repo}/actions/runs/{run_id}/artifacts
    2) Фильтрует артефакты с именами, начинающимися на "test-reports-".
    3) Скачивает zip по archive_download_url для каждого такого артефакта.
    4) Ищет внутри zip файлы .xml и парсит JUnit testcase с <failure>/<error>.
    5) Приводит к общему формату: {test_name: [{file, line_num, context, project}, ...]}.
    """

    def __init__(self, *, github_get_json: Callable[[str], requests.Response],
                 github_get_zip: Callable[[str], requests.Response], owner: str) -> None:
        self.github_get_json = github_get_json
        self.github_get_zip = github_get_zip
        self.owner = owner

    @staticmethod
    def _tag_local(tag: str) -> str:
        return tag.split('}', 1)[-1] if '}' in tag else tag

    def _list_run_artifacts(self, repo: str, run_id: int) -> List[Dict[str, Any]]:
        url = f'https://api.github.com/repos/{self.owner}/{repo}/actions/runs/{run_id}/artifacts'
        try:
            resp = self.github_get_json(url)
            data = resp.json() or {}
            return list(data.get('artifacts', []) or [])
        except requests.RequestException as e:
            print(f"⚠ Ошибка получения списка артефактов для run {run_id}: {e}")
            return []

    def _parse_junit_zip(self, zip_bytes: bytes, project_name: str) -> Tuple[Dict[str, List[Dict]], bool]:
        """Парсит zip с JUnit XML. Возвращает (failed_details, found_any_junit).

        found_any_junit=True, если хотя бы один .xml содержал <testcase>.
        """
        failed: Dict[str, List[Dict]] = {}
        found_any_junit = False
        # Локальный порядок появления testcases внутри данного zip (по test_key, без message)
        seen_tc_order: Dict[str, int] = {}
        local_pos = 0
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                for name in z.namelist():
                    if not name.lower().endswith('.xml'):
                        continue
                    try:
                        with z.open(name) as f:
                            xml_bytes = f.read()
                        # Пытаемся распарсить XML
                        root = ET.fromstring(xml_bytes)
                    except Exception:
                        # Не JUnit или повреждённый xml — пропускаем
                        continue

                    # Ищем testcases по дереву без учёта namespace
                    has_testcase = False
                    for tc in root.iter():
                        if self._tag_local(tc.tag) != 'testcase':
                            continue
                        has_testcase = True
                        classname = (tc.attrib.get('classname') or '').strip()
                        tname = (tc.attrib.get('name') or '').strip()
                        if classname and tname:
                            test_key = f"{classname}::{tname}"
                        else:
                            test_key = tname or classname or 'unknown'

                        # Фиксируем порядок test_key сразу при встрече testcase
                        if test_key not in seen_tc_order:
                            seen_tc_order[test_key] = local_pos
                            local_pos += 1

                        # Собираем все failure/error для данного testcase
                        for child in list(tc):
                            tag = self._tag_local(child.tag)
                            if tag not in ('failure', 'error'):
                                continue
                            message = (child.attrib.get('message') or '').strip()
                            details_text = (child.text or '').strip()
                            context = f"\n{message}\n{details_text}\n---\n".strip('\n')

                            # Итоговое имя теста в нашем формате: "key | description"
                            test_name = f"{test_key} | {message}"
                            item = {
                                'file': name,
                                'line_num': 0,
                                'context': context,
                                'project': project_name,
                                # Порядок по test_key, а не по конкретному сообщению
                                'order_index': seen_tc_order.get(test_key, 10**9),
                            }
                            failed.setdefault(test_name, []).append(item)

                    if has_testcase:
                        found_any_junit = True
        except zipfile.BadZipFile:
            print("⚠ Повреждённый zip при парсинге junit артефакта")
        return failed, found_any_junit

    def extract(self, repo: str, run_id: int, run_info: Optional[Dict[str, Any]] = None
                ) -> Tuple[Dict[str, List[Dict]], bool]:
        artifacts = self._list_run_artifacts(repo, run_id)
        if not artifacts:
            print(f"ℹ️ Для run {run_id} артефактов не найдено")
            return {}, False

        # Берём все артефакты test-reports-* (не истёкшие)
        report_artifacts = [a for a in artifacts if str(a.get('name', '')).startswith('test-reports-') and not a.get('expired')]
        if not report_artifacts:
            print(f"ℹ️ Для run {run_id} нет артефактов вида 'test-reports-*'")
            return {}, False

        combined: Dict[str, List[Dict]] = {}
        found_any_junit = False
        # Глобальный порядок тестов для всего run (по мере обхода артефактов и их содержимого)
        global_pos = 0
        first_seen_order: Dict[str, int] = {}

        for art in report_artifacts:
            name = str(art.get('name', ''))
            project = name[len('test-reports-'):] if name.startswith('test-reports-') else name
            dl_url = art.get('archive_download_url')
            if not dl_url:
                continue
            try:
                resp = self.github_get_zip(dl_url)
                zip_bytes = resp.content
            except requests.RequestException as e:
                print(f"⚠ Ошибка скачивания артефакта '{name}' для run {run_id}: {e}")
                continue

            parsed, has_junit = self._parse_junit_zip(zip_bytes, project)
            if has_junit:
                found_any_junit = True
            if parsed:
                # Мержим результаты без перезаписи order_index — он уже отражает порядок testcases
                for k, v in parsed.items():
                    if k not in first_seen_order:
                        first_seen_order[k] = global_pos
                        global_pos += 1
                    combined.setdefault(k, []).extend(v)

        # Если вообще не нашли junit — трактуем как отсутствие результатов
        has_no_tests = not found_any_junit
        if has_no_tests:
            print(f"⚠ В артефактах run {run_id} не обнаружено JUnit отчётов")
        return combined, has_no_tests


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

        # Инициализация экстракторов результатов тестов
        self.log_extractor = LogTestResultsExtractor(download_logs_func=self.download_logs)
        self.artifacts_extractor = ArtifactsTestResultsExtractor(
            github_get_json=self.github_get,
            github_get_zip=self.github_get_zip,
            owner=self.owner
        )

        # Настройки сохранения txt логов
        self.save_logs = False
        self.log_save_dir: Optional[Path] = None
        # Флаг: принудительно переизвлекать результаты, игнорируя кэш
        self.force_refresh_cache: bool = False

    def configure_cache(self, save_logs: bool = False, log_save_dir: Optional[Path] = None,
                        force_refresh_cache: bool = False):
        """Конфигурирует поведение кэша и сохранение txt-логов.

        Параметры:
        - save_logs: сохранять ли txt файлы из zip логов на диск
        - log_save_dir: директория для сохранения txt
        - force_refresh_cache: если True — игнорировать записи в кэше и переизвлекать заново
        """
        self.save_logs = bool(save_logs)
        self.log_save_dir = log_save_dir
        self.force_refresh_cache = bool(force_refresh_cache)

    # --- JSON sidecar теперь обрабатывается в ArtifactCache --- #

    def github_get(self, url: str, **kwargs) -> requests.Response:
        """Выполняет GET запрос к GitHub API с обработкой ошибок."""
        response = requests.get(url, headers=self.headers, **kwargs)
        response.raise_for_status()
        return response

    def github_get_zip(self, url: str, **kwargs) -> requests.Response:
        """Выполняет GET запрос к GitHub API для скачивания бинарных артефактов (zip).

        Некоторые эндпоинты (archive_download_url) корректно отдают редирект на S3 при стандартном
        Accept: application/vnd.github.v3+json. Если сервер отвечает 415/406, пробуем повторить
        запрос с Accept: application/octet-stream.
        """
        try:
            response = requests.get(url, headers=self.headers, **kwargs)
            response.raise_for_status()
            return response
        except requests.HTTPError as e:
            status = getattr(e.response, 'status_code', None)
            if status in (415, 406):
                headers_zip = self.headers.copy()
                headers_zip['Accept'] = 'application/octet-stream'
                response = requests.get(url, headers=headers_zip, **kwargs)
                response.raise_for_status()
                return response
            raise

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

    def _load_or_extract_run_details(self, repo: str, run_id: int,
                                     run_info: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, List[Dict]], bool]:
        """Пытается загрузить распарсенные детали тестов из sidecar или извлечь их через экстракторы.

        Алгоритм:
        1) Загрузить кешированные результаты из MongoDB.
        2) Если в кэше валидно — вернуть. Иначе попробовать снова извлечь: сначала из артефактов,
           при необходимости fallback на логи.
        3) Сохранить, что получилось, в MongoDB.
        """
        # 1) Кеш
        if not self.force_refresh_cache:
            cached = self.artifact_cache.load_parsed_sidecar(self.owner, repo, run_id)
            if cached is not None:
                details_cached, has_no_tests_cached = cached
                print(f"💾 Кеш для run {run_id}: has_no_tests={has_no_tests_cached}, details_count={len(details_cached) if details_cached else 0}")
                # Валидно ТОЛЬКО если has_no_tests=False (т.е. результаты тестов найдены)
                is_valid_cached = not has_no_tests_cached
                if is_valid_cached:
                    print(f"✅ Используем кешированные данные для run {run_id}")
                    return details_cached or {}, has_no_tests_cached
                else:
                    print(f"♻️ Кеш для run {run_id} невалиден (has_no_tests=True), пробуем переизвлечь…")
        else:
            print(f"🧹 Принудительное обновление кэша для run {run_id}: игнорируем сохранённые данные")

        # 2) Основной способ — артефакты
        details, has_no_tests = self.artifacts_extractor.extract(repo, run_id, run_info=run_info)
        print(f"📦 Артефакты для run {run_id}: has_no_tests={has_no_tests}, details_count={len(details) if details else 0}")

        # 2b) Fallback — логи
        # Валидно, если JUnit найден (has_no_tests=False), даже когда падений нет.
        is_valid = (not has_no_tests) or bool(details)
        if not is_valid:
            print(f"🔁 Fallback: пробуем извлечь результаты тестов из логов для run {run_id}")
            alt_details, alt_has_no_tests = self.log_extractor.extract(repo, run_id, run_info=run_info)
            print(f"📝 Логи для run {run_id}: has_no_tests={alt_has_no_tests}, details_count={len(alt_details) if alt_details else 0}")
            # Принимаем результаты логов, если там обнаружены тесты (alt_has_no_tests == False),
            # даже если падений нет (alt_details пустой)
            if not alt_has_no_tests:
                details, has_no_tests = alt_details, alt_has_no_tests
                print(f"✅ Используем результаты из логов для run {run_id}")

        # 3) Сохраняем в Mongo
        print(f"💾 Сохраняем в кэш run {run_id}: has_no_tests={has_no_tests}, details_count={len(details) if details else 0}")
        self.artifact_cache.save_parsed_sidecar(self.owner, repo, run_id, details, has_no_tests)
        return details or {}, has_no_tests

    # --- Общая логика парсинга zip логов тестов --- #

    def _parse_zip_internal(self, zip_bytes: bytes, *, detect_no_tests: bool, test_name_joiner: str
                             ) -> Tuple[Dict[str, List[Dict]], bool]:
        """Единый парсер zip логов (делегирует в LogTestResultsExtractor)."""
        # Делегируем парсинг новому классу-экстрактору
        return self.log_extractor.parse_zip(zip_bytes, detect_no_tests=detect_no_tests, test_name_joiner=test_name_joiner)

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

                    # Загружаем или извлекаем детали единожды через хелпер
                    details, has_no_tests = self._load_or_extract_run_details(repo, run['id'])
                    if has_no_tests:
                        print(f"⚠ Run {run['id']} не содержит результатов тестов (No test results), пропускаем")
                        continue
                    # Сохраняем распарсенные детали в объект run, чтобы не парсить повторно позже
                    # Даже если details пустой, это валидное состояние (все тесты прошли).
                    run['parsed_test_details'] = details or {}

                    # Если дошли сюда - run валидный (даже при отсутствии падений)
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
            - summary: composite_key -> set(failed_tests), где composite_key = f"{sha}_{run_id}"
            - meta: composite_key -> {'sha', 'run_id', 'title', 'ts', 'concl', 'link', 'branch'}
            - all_test_details: test_name -> list of details
        """
        runs = self.get_recent_runs(repo, branch, max_runs)
        if not runs:
            return {}, {}, {}

        summary, meta, all_test_details = {}, {}, {}

        for i, run in enumerate(runs):
            sha = run['head_sha']
            run_id = run['id']
            # Составной ключ для уникальной идентификации каждого билда
            composite_key = f"{sha}_{run_id}"
            
            title = self.get_commit_title(repo, sha) or sha[:7]
            branch_name = run.get('head_branch')
            ts = datetime.fromisoformat(
                (run.get('run_started_at') or run.get('created_at')).replace('Z', '+00:00')
            ).strftime('%Y-%m-%d %H:%M:%S')
            concl = run.get('conclusion')
            run_link = f"https://github.com/{self.owner}/{repo}/actions/runs/{run_id}"

            print(f"🔍 {title} | {branch_name} | {ts} | Статус: {concl} | {run_link}")

            # Используем предварительно распарсенные детали
            # Все невалидные билды уже отфильтрованы в get_recent_runs
            test_details = run.get('parsed_test_details', {})

            # Сохраняем порядок ключей как в исходных данных (insertion order словаря)
            failed_order = list(test_details.keys()) if test_details else []
            failed = set(failed_order)
            if test_details:
                all_test_details.update(test_details)

            summary[composite_key] = failed
            meta[composite_key] = {
                'sha': sha,
                'run_id': run_id,
                'title': title,
                'ts': ts,
                'concl': concl,
                'link': run_link,
                'branch': branch_name,
                'order': failed_order
            }

        return summary, meta, all_test_details

    def get_master_failed_tests(self, repo: str, master_branch: str = 'master') -> Set[str]:
        """Получает список падающих тестов в master ветке."""
        master_run = self.get_latest_completed_run(repo, master_branch)
        if not master_run:
            return set()
        # Используем общий путь с приоритетом артефактов и учётом force_refresh_cache
        details, has_no_tests = self._load_or_extract_run_details(repo, master_run['id'])
        if has_no_tests or not details:
            return set()
        return set(details.keys())
