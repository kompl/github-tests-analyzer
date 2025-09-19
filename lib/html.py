from typing import Optional, Dict, List, Any
from jinja2 import Environment, FileSystemLoader, Template
from pathlib import Path
from datetime import datetime
import os
import json
import html as html_lib

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

    def add_run_section(self, title: str, items: List[Any], max_show: int = 10) -> None:
        """Добавляет секцию в текущий run."""
        if not self.current_run:
            raise ValueError("Сначала вызовите start_run_section")
        # Важно: сохраняем порядок элементов как пришёл (без сортировок)
        ordered_items = list(items)
        # Формируем древовидную структуру: разбиваем по '::' на произвольную глубину
        grouped: Dict[str, List[Dict[str, str]]] = {}
        tree: List[Dict[str, Any]] = []

        def find_or_create(nodes: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
            for n in nodes:
                if n['name'] == name:
                    return n
            node = {'name': name, 'children': [], 'leaves': []}
            nodes.append(node)
            return node

        default_group_name = 'Без префикса'
        for item in ordered_items:
            # item может быть строкой (старый формат) или словарём {'display': HTML, 'raw': исходное имя теста}
            if isinstance(item, dict):
                display_html = item.get('display', '')
                raw_value = item.get('raw', display_html)
            else:
                display_html = item
                raw_value = item
            # raw_value может содержать HTML/символы — приводим к базовой строке для группировки
            raw = html_lib.unescape(str(raw_value))
            base = raw.split(' — ', 1)[0]
            clean_item = base.split(' (', 1)[0]
            parts = clean_item.split('::') if '::' in clean_item else [clean_item]
            # для обратной совместимости первой группировки
            group_key = parts[0]
            grouped.setdefault(group_key, []).append({'item': display_html, 'clean_item': clean_item})

            # строим глубокое дерево: последний сегмент НЕ становится отдельной нодой — это лист
            parent_parts = parts[:-1] if len(parts) > 1 else [default_group_name]
            nodes = tree
            for part in parent_parts:
                node = find_or_create(nodes, part)
                nodes = node['children']
            # Добавляем лист к найденному/созданному родителю
            # На этом уровне нет отдельной ноды для последнего сегмента
            # Ищем последний родитель снова (nodes сейчас указывает на children последнего родителя)
            # Поэтому восстановим ссылку на сам последний родитель
            # (последний созданный/найденный node хранится в переменной node)
            node['leaves'].append({'item': display_html, 'clean_item': clean_item})

        # Подсчёт количества листьев в поддереве и упорядочивание узлов
        def compute_total_and_sort(nodes: List[Dict[str, Any]]) -> int:
            total = 0
            for n in nodes:
                # сначала сортируем детей и считаем их total
                child_total = compute_total_and_sort(n['children']) if n['children'] else 0
                leaf_count = len(n['leaves'])
                n['total'] = child_total + leaf_count
                total += n['total']
            return total

        compute_total_and_sort(tree)
        self.current_run['sections'].append({
            'title': title,
            'tests': ordered_items,
            'grouped': grouped,
            'tree': tree,
            'total': len(ordered_items),
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
