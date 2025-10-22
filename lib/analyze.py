#!/usr/bin/env python3
from typing import Dict, Set, List, Any


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
        ordered_keys = list(self.summary.keys())  # Раны уже в хронологическом порядке (составные ключи)
        test_states = {}  # test_name -> [True/False] для каждого рана

        for test in all_tests:
            states = []
            for composite_key in ordered_keys:
                states.append(test in self.summary[composite_key])
            test_states[test] = states

        # Анализируем паттерны
        stable_failing = {}
        fixed_tests = {}
        flaky_tests = {}

        for test, states in test_states.items():
            behavior = self._analyze_test_pattern(test, states, ordered_keys)

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

    def _analyze_test_pattern(self, test_name: str, states: List[bool], composite_keys: List[str]) -> Dict[str, Any]:
        """Анализирует паттерн поведения одного теста.
        
        Args:
            test_name: имя теста
            states: список состояний (True=failed, False=passed)
            composite_keys: список составных ключей вида 'sha_runid'
        """
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
                composite_key = composite_keys[i]
                meta_info = self.meta[composite_key]
                failed_runs.append({
                    'sha': meta_info.get('sha', composite_key.split('_')[0]),
                    'composite_key': composite_key,
                    'meta': meta_info,
                    'run_number': i + 1
                })

        # Ищем ссылку на PR/коммит после последнего упавшего run
        next_pr_link = None
        next_commit_info = None
        if last_fail_idx is not None and last_fail_idx + 1 < total_runs:
            next_composite_key = composite_keys[last_fail_idx + 1]
            next_run_meta = self.meta.get(next_composite_key)
            if next_run_meta:
                # Берем ссылку на run (который содержит информацию о коммите)
                next_pr_link = next_run_meta.get('link')
                next_sha = next_run_meta.get('sha', next_composite_key.split('_')[0])
                next_commit_info = {
                    'sha': next_sha[:7],
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
        prev_key = None

        for composite_key, curr in self.summary.items():
            added = curr - prev
            removed = prev - curr
            only_here = curr - self.master_failed if self.master_failed else set()
            
            # Извлекаем SHA для логирования
            sha = self.meta[composite_key].get('sha', composite_key.split('_')[0])
            print(f"📊 Диф для {sha[:7]}: +{len(added)} -{len(removed)} (уникальных: {len(only_here)})")

            diffs.append({
                'sha': sha,
                'composite_key': composite_key,
                'meta': self.meta[composite_key],
                'order': self.meta.get(composite_key, {}).get('order', []),
                'prev_order': self.meta.get(prev_key, {}).get('order', []) if prev_key else [],
                'added': added,
                'removed': removed,
                'only_here': only_here,
                'current': curr
            })

            prev = curr
            prev_key = composite_key

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
