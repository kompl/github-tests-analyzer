from pathlib import Path
import json
from datetime import datetime
import zipfile
import io

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
        total_size = sum(item.get('size_bytes', item.get('size', 0)) for item in self.metadata.values())

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

    def save_txt_from_zip(self, zip_bytes, save_dir, run_prefix: str = ""):
        """Извлекает и сохраняет txt файлы из zip-архива в указанную директорию."""
        if not zip_bytes:
            return 0
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        saved_count = 0
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                for name in z.namelist():
                    if name.lower().endswith('.txt'):
                        safe_name = name.replace('/', '_').replace('\\', '_')
                        if run_prefix:
                            safe_name = f"{run_prefix}_{safe_name}"
                        txt_path = save_dir / safe_name
                        with z.open(name) as f:
                            content = f.read()
                            txt_path.write_bytes(content)
                            saved_count += 1
        except Exception as e:
            print(f"⚠ Ошибка сохранения txt файлов: {e}")
        return saved_count

    # --- Sidecar JSON рядом с zip: распарсенные детали --- #
    def _get_parsed_json_path(self, owner: str, repo: str, run_id: int) -> Path:
        """Возвращает путь к sidecar JSON для распарсенных деталей рядом с zip."""
        cache_key = self._get_cache_key(owner, repo, run_id)
        zip_path = self._get_cache_path(cache_key)
        return zip_path.with_suffix('.parsed.json')

    def load_parsed_sidecar(self, owner: str, repo: str, run_id: int):
        """Пытается загрузить (details, has_no_tests) из sidecar JSON. Возвращает None при отсутствии/ошибке."""
        path = self._get_parsed_json_path(owner, repo, run_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                return None
            if 'details' not in data or 'has_no_tests' not in data:
                return None
            details = data.get('details') or {}
            has_no_tests = bool(data.get('has_no_tests'))
            return details, has_no_tests
        except Exception as e:
            print(f"⚠ Ошибка загрузки sidecar JSON {path}: {e}")
            return None

    def save_parsed_sidecar(self, owner: str, repo: str, run_id: int, details, has_no_tests: bool) -> bool:
        """Сохраняет sidecar JSON с распарсенными деталями и флагом no-tests рядом с zip."""
        path = self._get_parsed_json_path(owner, repo, run_id)
        try:
            payload = {
                'schema': 1,
                'owner': owner,
                'repo': repo,
                'run_id': run_id,
                'created_at': datetime.now().isoformat(),
                'has_no_tests': bool(has_no_tests),
                'details': details or {}
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            return True
        except Exception as e:
            print(f"⚠ Ошибка сохранения sidecar JSON {path}: {e}")
            return False
