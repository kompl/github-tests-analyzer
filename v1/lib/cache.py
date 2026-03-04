from pathlib import Path
from datetime import datetime
import zipfile
import io
import os
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    # Пытаемся импортировать pymongo. В тестах можно передать mongomock.MongoClient через параметр client.
    from pymongo import MongoClient, ASCENDING
except Exception:  # pragma: no cover - позволяeт тестировать без установленного pymongo
    MongoClient = None  # type: ignore
    ASCENDING = 1  # type: ignore


# ---------- ХРАНИЛИЩЕ РЕЗУЛЬТАТОВ ПАРСИНГА В MONGODB ---------- #
class ArtifactCache:
    """
    Хранилище распарсенных результатов тестовых логов в MongoDB.

    - Не сохраняет zip архивы.
    - Сохраняет только распарсенные данные: details и флаг has_no_tests.
    - Разделение по проектам осуществляется полями owner/repo.

    Параметры:
      mongo_uri: строка подключения к MongoDB. Если не указана, берётся из переменной окружения MONGO_URI
                 или по умолчанию mongodb://localhost:27017
      db_name: имя базы данных
      collection_name: имя коллекции
      client: готовый MongoClient (в тестах можно передавать mongomock.MongoClient)
    """

    def __init__(
        self,
        mongo_uri: Optional[str] = None,
        db_name: str = "filedecorator",
        collection_name: str = "parsed_results",
        client: Optional[Any] = None,
    ) -> None:
        if client is not None:
            self.client = client
        else:
            if MongoClient is None:
                raise RuntimeError(
                    "pymongo не установлен и не передан client. Установите pymongo или передайте совместимый клиент."
                )
            mongo_uri = mongo_uri or os.getenv("MONGO_URI", "mongodb://localhost:27017")
            self.client = MongoClient(mongo_uri)

        self.db = self.client[db_name]
        self.coll = self.db[collection_name]
        # Уникальный индекс на (owner, repo, run_id)
        try:
            self.coll.create_index([("owner", ASCENDING), ("repo", ASCENDING), ("run_id", ASCENDING)], unique=True)
        except Exception:
            # В mongomock или при гонках индекс может уже существовать
            pass

    # ---------- Утилиты кодирования/декодирования details ---------- #
    @staticmethod
    def _encode_details(details: Optional[Dict[str, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
        """
        Преобразует словарь {test_name: [items]} в список документов для безопасного хранения в MongoDB.
        Это позволяет хранить ключи с точками, пробелами и любыми символами.
        """
        result: List[Dict[str, Any]] = []
        if not details:
            return result
        for test_name, items in details.items():
            result.append({
                "test_name": str(test_name),
                "items": list(items or []),
            })
        return result

    @staticmethod
    def _decode_details(details_list: Optional[List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
        """Обратное преобразование из списка документов в словарь."""
        result: Dict[str, List[Dict[str, Any]]] = {}
        if not details_list:
            return result
        for rec in details_list:
            name = rec.get("test_name")
            items = rec.get("items") or []
            if name is not None:
                result[str(name)] = list(items)
        return result

    # ---------- Публичные методы ---------- #
    def save_parsed_sidecar(
        self,
        owner: str,
        repo: str,
        run_id: int,
        details: Optional[Dict[str, List[Dict[str, Any]]]],
        has_no_tests: bool,
    ) -> bool:
        """
        Сохраняет распарсенные результаты (details, has_no_tests) в MongoDB.
        При повторном сохранении — обновляет существующую запись (upsert).
        """
        payload = {
            "schema": 2,  # версия схемы для Mongo-хранения
            "owner": owner,
            "repo": repo,
            "run_id": int(run_id),
            "created_at": datetime.now().isoformat(),
            "has_no_tests": bool(has_no_tests),
            "details_list": self._encode_details(details),
        }
        try:
            res = self.coll.update_one(
                {"owner": owner, "repo": repo, "run_id": int(run_id)},
                {"$set": payload},
                upsert=True,
            )
            # acknowledged True почти всегда; учитываем успешность операции
            return bool(res.acknowledged)
        except Exception as e:
            print(f"⚠ Ошибка сохранения результатов в MongoDB: {e}")
            return False

    def load_parsed_sidecar(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> Optional[Tuple[Dict[str, List[Dict[str, Any]]], bool]]:
        """
        Загружает распарсенные результаты из MongoDB.
        Возвращает кортеж (details, has_no_tests) или None, если данных нет.
        """
        try:
            doc = self.coll.find_one({"owner": owner, "repo": repo, "run_id": int(run_id)})
            if not doc:
                return None
            details = self._decode_details(doc.get("details_list"))
            has_no_tests = bool(doc.get("has_no_tests", False))
            return details, has_no_tests
        except Exception as e:
            print(f"⚠ Ошибка загрузки результатов из MongoDB: {e}")
            return None

    def find_earliest_run_with_tests(
        self,
        owner: str,
        repo: str,
        test_names: List[str],
        candidate_run_ids: Optional[List[int]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        For each test_name find the earliest run_id (optionally restricted to candidate_run_ids)
        where this test appears in details_list.

        Returns {test_name: {"run_id": int, "created_at": str}}.
        """
        result: Dict[str, Dict[str, Any]] = {}
        base_query: Dict[str, Any] = {"owner": owner, "repo": repo, "has_no_tests": False}
        if candidate_run_ids:
            base_query["run_id"] = {"$in": [int(rid) for rid in candidate_run_ids]}

        for test_name in test_names:
            query = {**base_query, "details_list.test_name": test_name}
            try:
                doc = self.coll.find_one(query, {"run_id": 1, "created_at": 1}, sort=[("run_id", ASCENDING)])
                if doc:
                    result[test_name] = {
                        "run_id": doc["run_id"],
                        "created_at": doc.get("created_at", ""),
                    }
            except Exception as e:
                print(f"⚠ Ошибка поиска истории теста в MongoDB: {e}")
        return result

    # ---------- Вспомогательная утилита: сохранить txt из zip на диск (опционально) ---------- #
    def save_txt_from_zip(self, zip_bytes: bytes, save_dir: Union[str, Path], run_prefix: str = "") -> int:
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
