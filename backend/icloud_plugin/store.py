"""Состояние плагина — файл state.json в служебной папке (PULSE_PLUGIN_DATA). Запись атомарная (временный файл и
rename), права 0600. Сессии Apple лежат рядом, в sessions/<uid>/ (их пишет pyicloud); пароли не хранятся нигде.

    {"accounts": {"<uid>": {"uid", "apple_id", "name", "expired", "created_at"}},
     "mounts": [{"name", "account", "folder_id", "folder_path"}]}
"""

import json
import os
import tempfile
import threading
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "state.json"
        self._lock = threading.RLock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def change(self, fn):
        """Прочитать, поменять функцией fn(data) и записать — под замком. Возвращает то, что вернула fn."""
        with self._lock:
            data = self.load()
            result = fn(data)
            self._write(data)
            return result

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
