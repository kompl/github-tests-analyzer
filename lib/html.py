from typing import Optional, Dict, List, Any
from jinja2 import Environment, FileSystemLoader, Template
from pathlib import Path
from datetime import datetime
import os
import json

class HtmlReportBuilder:
    def __init__(
        self,
        filename: str,
        repo_name: str,
        branch_name: str
    ) -> None:
        self.filename: str = filename
        self.repo_name: str = repo_name
        self.branch_name: str = branch_name
        self.test_details: Dict[str, List[Dict[str, Any]]] = {}  # path -> детали теста
        self.counter: int = 0  # Счетчик для уникальных ID
        self.runs: List[Dict[str, Any]] = []  # Список run'ов с их секциями
        self.current_run: Optional[Dict[str, Any]] = None  # Текущий run
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)

    def add_test_details(self, test_details: Dict[str, List[Dict[str, Any]]]) -> None:
        """Добавляет детали тестов для использования в кнопках."""
        self.test_details.update(test_details)

    def start_run_section(self, commit_info: Dict[str, str]) -> None:
        """Начинает новую секцию для run'а."""
        if self.current_run:
            self.finish_run_section()
        self.current_run = {
            'commit_info': commit_info,
            'sections': []
        }

    def add_run_section(self, title: str, items: List[str], max_show: int = 10) -> None:
        """Добавляет секцию в текущий run."""
        if not self.current_run:
            raise ValueError("Сначала вызовите start_run_section")
        sorted_items = sorted(items)
        self.current_run['sections'].append({
            'title': title,
            'tests': sorted_items,
            'total': len(sorted_items),
            'max_show': max_show
        })

    def finish_run_section(self) -> None:
        """Завершает текущую секцию run'а."""
        if self.current_run:
            self.runs.append(self.current_run)
            self.current_run = None

    def add_section(
        self,
        title: str,
        items: List[str],
        max_show: int = 10,
        commit_info: Optional[Dict[str, str]] = None
    ) -> None:
        """Добавляет обычную секцию (для первого запуска)."""
        # Для совместимости: создаём отдельный run для одиночной секции
        self.start_run_section(commit_info or {
            'title': title,
            'branch': self.branch_name,
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'concl': 'unknown',
            'link': ''
        })
        self.add_run_section(title, items, max_show)
        self.finish_run_section()

    def write(self) -> None:
        # Завершаем последний run, если есть
        if self.current_run:
            self.finish_run_section()

        # Подготавливаем данные для JS
        details_js_data: Dict[str, str] = {}
        for test_path, details in self.test_details.items():
            details_text = ""
            for detail in details:
                details_text += f"Файл: {detail['file']}\nСтрока: {detail['line_num']}\n\nКонтекст:\n{detail['context']}\n\n---\n\n"
            details_js_data[test_path] = details_text
        # Сериализуем в JSON заранее, чтобы безопасно вставить в шаблон
        details_js_json: str = json.dumps(details_js_data, ensure_ascii=False)
        # Защита от преждевременного закрытия <script> при наличии "</script>" в тексте
        details_js_json_safe: str = details_js_json.replace('</', '<\\/')

        template_dir: str = os.path.dirname(os.path.abspath(__file__))  # Директория скрипта
        env: Environment = Environment(loader=FileSystemLoader(template_dir))
        template: Template = env.get_template('report_template.jinja')

        # Подготавливаем данные для рендеринга
        report_date: str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cache_stats: Dict[str, int] = {'total_cached': 0, 'total_size_mb': 0}  # Замените на реальные, как в оригинале

        html_content: str = template.render(
            repo_name=self.repo_name,
            branch_name=self.branch_name,
            report_date=report_date,
            cache_stats=cache_stats,
            runs=self.runs,
            test_details=self.test_details,  # Для проверки в шаблоне
            details_js_data=details_js_data,
            details_js_json=details_js_json_safe
        )

        Path(self.filename).write_text(html_content, encoding='utf-8')
        print(f"✅ Сгенерирован HTML-отчёт: {self.filename}")
