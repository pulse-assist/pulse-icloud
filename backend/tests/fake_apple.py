"""iCloud в памяти — подменяет apple.Apple в тестах: вход, коды, дерево iCloud Drive."""

import itertools

from icloud_plugin.apple import AppleError, SessionExpired

PASSWORD = "correct-horse"
CODE = "123456"


class FakeCloud:
    """Аккаунты и их Drive. Узлы — как их отдаёт apple.node_of."""

    def __init__(self):
        self.seq = itertools.count(1)
        self.needs_code = True
        self.locked = False
        self.sessions: set[str] = set()       # apple_id с доверенной сессией
        self.revoked: set[str] = set()        # Apple отозвал сессию
        self.adp: set[str] = set()            # «Расширенная защита данных» без доступа из веба
        self.drives: dict[str, dict] = {}     # apple_id → {id: node}
        self.contents: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def drive(self, apple_id: str) -> dict:
        if apple_id not in self.drives:
            nodes = {"root": {"id": "root", "type": "folder", "name": "root", "parent": None}}
            self.drives[apple_id] = nodes
            docs = self.add(apple_id, "root", "Документы", folder=True)
            scans = self.add(apple_id, docs, "Сканы", folder=True)
            self.add(apple_id, scans, "паспорт.pdf", b"%PDF-1.4")
            self.add(apple_id, docs, "план.txt", "план на неделю".encode())
            self.add(apple_id, docs, "план.txt", b"second")             # то же имя в той же папке
            self.add(apple_id, "root", "Отчёт.pages", b"PK pages")
        return self.drives[apple_id]

    def add(self, apple_id: str, parent: str, name: str, content: bytes | None = None, folder: bool = False) -> str:
        node_id = f"{'FOLDER' if folder else 'FILE'}::{next(self.seq)}"
        self.drives[apple_id][node_id] = {"id": node_id, "type": "folder" if folder else "file", "name": name, "parent": parent,
                                          "etag": "e1", "docwsid": node_id, "zone": "com.apple.CloudDocs"}
        if not folder:
            self.contents[node_id] = content or b""
        return node_id

    def node(self, apple_id: str, node_id: str) -> dict:
        raw = self.drive(apple_id).get(node_id)
        if raw is None:
            raise AppleError("узел не найден")
        size = len(self.contents[node_id]) if raw["type"] == "file" else None
        return {**{k: raw[k] for k in ("id", "type", "name")}, "docwsid": raw.get("docwsid"), "etag": raw.get("etag"),
                "zone": raw.get("zone"), "size": size, "modified": "2026-11-01T10:00:00Z", "raw": {"drivewsid": node_id}}


class FakeApple:
    def __init__(self, cloud: FakeCloud, apple_id: str, trusted: bool):
        self.cloud = cloud
        self.apple_id = apple_id
        self.trusted = trusted
        self.delivery_ = "unknown"
        self.password_forgotten = False

    # --- вход
    @property
    def needs_code(self) -> bool:
        return not self.trusted

    @property
    def delivery(self) -> str:
        return self.delivery_

    def request_code(self) -> str:
        self.delivery_ = "trusted_device"
        return self.delivery_

    def request_sms(self) -> str:
        self.delivery_ = "sms"
        return self.delivery_

    def submit_code(self, code: str) -> bool:
        if code != CODE:
            return False
        self.trusted = True
        self.cloud.sessions.add(self.apple_id)
        return True

    def forget_password(self) -> None:
        self.password_forgotten = True

    def info(self) -> dict:
        return {"name": "Artem D.", "dsid": "1"}

    def logout(self) -> None:
        self.cloud.sessions.discard(self.apple_id)

    # --- Drive
    def _check(self) -> None:
        if self.apple_id in self.cloud.revoked:
            raise SessionExpired("Apple просит войти заново")
        if self.apple_id in self.cloud.adp:
            raise AppleError("iCloud Drive закрыт для веба: включена «Расширенная защита данных»")

    def root(self) -> dict:
        self._check()
        return self.cloud.node(self.apple_id, "root")

    def get(self, node_id: str) -> dict:
        self._check()
        return self.cloud.node(self.apple_id, node_id)

    def children(self, folder: dict) -> list[dict]:
        self._check()
        self.cloud.calls.append(("children", folder["id"]))
        return [self.cloud.node(self.apple_id, i) for i, n in self.cloud.drive(self.apple_id).items() if n["parent"] == folder["id"]]

    def read(self, node: dict, offset: int = 0, length: int | None = None):
        self._check()
        data = self.cloud.contents[node["id"]]
        end = None if length is None else offset + length
        return iter([data[offset:end]])

    def upload(self, folder: dict, name: str, stream) -> None:
        self._check()
        if name == "сбой.txt":
            raise AppleError("загрузка не удалась")
        self.cloud.calls.append(("upload", folder["id"], name))
        existing = [n for n in self.cloud.drive(self.apple_id).values() if n["parent"] == folder["id"] and n["name"] == name]
        stem, dot, ext = name.rpartition(".")
        final = f"{stem} 2.{ext}" if existing and dot else name            # как iCloud: копия рядом, а не замена
        self.cloud.add(self.apple_id, folder["id"], final, stream.read())

    def mkdir(self, folder: dict, name: str) -> None:
        self._check()
        self.cloud.add(self.apple_id, folder["id"], name, folder=True)

    def rename(self, node: dict, name: str) -> None:
        self._check()
        self.cloud.calls.append(("rename", node["id"], name))
        self.cloud.drive(self.apple_id)[node["id"]]["name"] = name

    def move(self, node: dict, folder: dict) -> None:
        self._check()
        self.cloud.drive(self.apple_id)[node["id"]]["parent"] = folder["id"]

    def trash(self, node: dict) -> None:
        self._check()
        self.cloud.calls.append(("trash", node["id"]))
        self.cloud.drive(self.apple_id)[node["id"]]["parent"] = "TRASH"

    def storage(self) -> dict:
        return {"used": 5 * 1024**3, "total": 50 * 1024**3}


def factories(cloud: FakeCloud):
    def login(apple_id: str, password: str, directory):
        if cloud.locked:
            raise AppleError("Apple заблокировал вход в этот аккаунт")
        if password != PASSWORD:
            raise AppleError("Apple не принял Apple ID или пароль")
        cloud.calls.append(("login", apple_id, directory.name))
        directory.mkdir(parents=True, exist_ok=True)
        return FakeApple(cloud, apple_id, trusted=not cloud.needs_code)

    def resume(apple_id: str, directory):
        if apple_id not in cloud.sessions or apple_id in cloud.revoked or not directory.is_dir():
            raise SessionExpired("сессии нет")
        return FakeApple(cloud, apple_id, trusted=True)

    return login, resume
