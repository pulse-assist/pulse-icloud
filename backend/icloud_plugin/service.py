"""Аккаунты Apple, вход по шагам, подключения и хранилище @icloud.

Вход: Apple ID и пароль (их вводит владелец на странице плагина) → код с устройства Apple или по SMS → доверенная
сессия в sessions/<uid>. Пароль нигде не сохраняется; сессия живёт, пока её не отзовёт Apple (обычно около двух
месяцев), потом аккаунт помечается «вход истёк» и Пульс получает сигнал.

Подключение — папка в @icloud: весь iCloud Drive аккаунта или папка в нём. iCloud работает с id узлов, поэтому путь
переводится в узлы с коротким кэшем списков; одинаковые имена в одной папке различаются суффиксом «(2)».
"""

import hashlib
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pulse_plugin.fs import Exists, FileProvider, FsError, NotEmpty, NotFound, ReadOnly, entry

from .apple import Apple, AppleError, SessionExpired
from .store import Store

LOGIN_TTL_SEC = 10 * 60
LIST_TTL_SEC = 15.0
DRIVE_TITLE = "iCloud Drive"
_BAD_NAME = re.compile(r'[/\\<>:"|?*\x00]')
_APPLE_ID = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$|^\+?\d{7,15}$")


class UserError(Exception):
    """Ошибка, понятная владельцу и агенту (400)."""


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or name.startswith(".") or _BAD_NAME.search(name) or len(name) > 100:
        raise UserError("Имя папки — до 100 символов, без / \\ < > : \" | ? * и не с точки")
    return name


def uid_of(apple_id: str) -> str:
    return hashlib.sha1(apple_id.strip().lower().encode("utf-8")).hexdigest()[:12]


def is_folder(node: dict) -> bool:
    return node.get("type") != "file"


def visible(nodes: list[dict]) -> dict[str, dict]:
    """Узлы папки → имя в @icloud → узел. Одинаковые имена — с «(2)», служебные (наша замена файла) скрыты."""
    out: dict[str, dict] = {}
    taken: set[str] = set()
    for node in nodes:
        name = (node.get("name") or "без имени").replace("/", "∕").strip() or "без имени"
        if name.startswith(".pulse-old-"):
            continue
        stem, dot, ext = name.rpartition(".") if "." in name.strip(".") and not is_folder(node) else (name, "", "")
        candidate, n = name, 2
        while candidate.casefold() in taken:
            candidate, n = f"{stem} ({n}){dot}{ext}", n + 1
        taken.add(candidate.casefold())
        out[candidate] = node
    return out


def to_entry(name: str, node: dict) -> dict:
    folder = is_folder(node)
    return entry(name, "dir" if folder else "file", None if folder else node.get("size"), node.get("modified"))


class ICloud:
    def __init__(self, store: Store, sessions: Path, login: Callable[..., Apple] = Apple.login,
                 resume: Callable[..., Apple] = Apple.resume, on_expired: Callable[[dict], None] = lambda account: None,
                 clock=time.time):
        self.store = store
        self.sessions = Path(sessions)
        self._login_fn = login
        self._resume_fn = resume
        self.on_expired = on_expired
        self.clock = clock
        self._apples: dict[str, Apple] = {}
        self._apples_lock = threading.Lock()
        self._pending: dict | None = None        # вход, ждущий кода: {"apple", "apple_id", "delivery", "created_at"}
        self._last: dict | None = None           # чем закончился последний вход
        self._login_lock = threading.Lock()
        self._lists: dict[tuple, tuple[float, dict[str, dict]]] = {}
        self._lists_lock = threading.Lock()

    def session_dir(self, uid: str) -> Path:
        return self.sessions / uid

    # --- вход --------------------------------------------------------------------------------------------------

    def login_state(self) -> dict | None:
        pending = self._pending
        if pending and self.clock() - pending["created_at"] > LOGIN_TTL_SEC:
            self._pending = None
            self._last = {"status": "expired", "apple_id": pending["apple_id"], "error": "Код не введён за 10 минут — войдите заново"}
        if self._pending:
            return {"status": "code", "apple_id": self._pending["apple_id"], "delivery": self._pending["delivery"]}
        return self._last

    def start_login(self, apple_id: str, password: str) -> dict:
        apple_id = (apple_id or "").strip()
        if not _APPLE_ID.match(apple_id):
            raise UserError("Apple ID — это почта (или номер телефона) аккаунта Apple")
        if not password:
            raise UserError("Нужен пароль Apple ID")
        with self._login_lock:
            self._pending = None
            uid = uid_of(apple_id)
            try:
                apple = self._login_fn(apple_id, password, self.session_dir(uid))
            except AppleError as exc:
                self._last = {"status": "error", "apple_id": apple_id, "error": str(exc)}
                raise UserError(str(exc)) from exc
            if not apple.needs_code:
                return self._finish(apple, apple_id)
            try:
                delivery = apple.request_code()
            except AppleError as exc:
                self._last = {"status": "error", "apple_id": apple_id, "error": str(exc)}
                raise UserError(str(exc)) from exc
            self._pending = {"apple": apple, "apple_id": apple_id, "delivery": delivery, "created_at": self.clock()}
            self._last = None
            return self.login_state()

    def _need_pending(self) -> dict:
        self.login_state()
        if not self._pending:
            raise UserError("Вход не начат или устарел — введите Apple ID и пароль заново")
        return self._pending

    def send_sms(self) -> dict:
        with self._login_lock:
            pending = self._need_pending()
            try:
                pending["delivery"] = pending["apple"].request_sms()
            except AppleError as exc:
                raise UserError(str(exc)) from exc
            return self.login_state()

    def submit_code(self, code: str) -> dict:
        code = re.sub(r"\D", "", code or "")
        if len(code) != 6:
            raise UserError("Код — 6 цифр")
        with self._login_lock:
            pending = self._need_pending()
            try:
                ok = pending["apple"].submit_code(code)
            except AppleError as exc:
                raise UserError(str(exc)) from exc
            if not ok:
                raise UserError("Код не подошёл — проверьте и введите ещё раз")
            self._pending = None
            return self._finish(pending["apple"], pending["apple_id"])

    def cancel_login(self) -> None:
        self._pending = None
        self._last = None

    def _finish(self, apple: Apple, apple_id: str) -> dict:
        """Сессия доверенная: проверить, что Drive открывается, и завести аккаунт (новый — сразу с подключением)."""
        apple.forget_password()
        uid = uid_of(apple_id)
        try:
            apple.root()
        except AppleError as exc:
            self._last = {"status": "error", "apple_id": apple_id, "error": str(exc)}
            raise UserError(str(exc)) from exc
        info = apple.info()
        with self._apples_lock:
            self._apples[uid] = apple

        def apply(data: dict) -> bool:
            accounts = data.setdefault("accounts", {})
            new = uid not in accounts
            accounts[uid] = {**accounts.get(uid, {}), "uid": uid, "apple_id": apple_id, "name": info.get("name"),
                             "expired": False, "created_at": accounts.get(uid, {}).get("created_at", self.clock())}
            mounts = data.setdefault("mounts", [])
            if new and not any(m["account"] == uid for m in mounts):
                mounts.append({"name": self._free_name(mounts, DRIVE_TITLE if len(accounts) == 1 else f"{DRIVE_TITLE} ({apple_id})"),
                               "account": uid, "folder_id": None, "folder_path": None})
            return new

        self.store.change(apply)
        self.invalidate()
        self._last = {"status": "done", "apple_id": apple_id}
        return self._last

    # --- аккаунты ------------------------------------------------------------------------------------------------

    def _account(self, ref: str) -> dict:
        accounts = self.store.load().get("accounts") or {}
        ref = (ref or "").strip().lower()
        if not ref:
            if len(accounts) == 1:
                return next(iter(accounts.values()))
            raise UserError("Укажите аккаунт: Apple ID" if accounts else "Аккаунтов нет — войдите на странице плагина")
        for account in accounts.values():
            if ref in (account["uid"], account["apple_id"].lower()):
                return account
        raise UserError(f"Нет аккаунта {ref}")

    def apple(self, uid: str) -> Apple:
        with self._apples_lock:
            if uid in self._apples:
                return self._apples[uid]
        account = (self.store.load().get("accounts") or {}).get(uid)
        if account is None:
            raise NotFound("аккаунт отключён — подключение больше не работает")
        if account.get("expired"):
            raise FsError(f"вход в {account['apple_id']} истёк — войдите снова на странице «iCloud Drive»")
        try:
            apple = self._resume_fn(account["apple_id"], self.session_dir(uid))
        except SessionExpired:
            self._expire(uid)
            raise FsError(f"вход в {account['apple_id']} истёк — войдите снова на странице «iCloud Drive»") from None
        with self._apples_lock:
            self._apples[uid] = apple
        return apple

    def _expire(self, uid: str) -> None:
        with self._apples_lock:
            self._apples.pop(uid, None)

        def apply(data: dict) -> dict | None:
            account = (data.get("accounts") or {}).get(uid)
            if account is None or account.get("expired"):
                return None
            account["expired"] = True
            return dict(account)

        account = self.store.change(apply)
        if account:
            self.on_expired(account)

    def call(self, uid: str, fn, *args, **kwargs):
        """Операция с аккаунтом: истёкшая сессия помечает аккаунт, ошибки Apple — понятным текстом."""
        apple = self.apple(uid)
        try:
            return fn(apple, *args, **kwargs)
        except SessionExpired:
            self._expire(uid)
            account = (self.store.load().get("accounts") or {}).get(uid) or {}
            raise FsError(f"вход в {account.get('apple_id', 'аккаунт')} истёк — войдите снова на странице «iCloud Drive»") from None
        except AppleError as exc:
            raise FsError(str(exc)) from exc

    def accounts(self, space: bool = False) -> list[dict]:
        out = []
        for account in (self.store.load().get("accounts") or {}).values():
            item = {k: account.get(k) for k in ("uid", "apple_id", "name", "expired")}
            item["expired"] = bool(item["expired"])
            if space and not item["expired"]:
                try:
                    item["storage"] = self.call(account["uid"], lambda apple: apple.storage())
                except FsError as exc:
                    item["error"] = str(exc)
                    item["expired"] = bool((self.store.load().get("accounts") or {}).get(account["uid"], {}).get("expired"))
            out.append(item)
        return out

    def logout(self, ref: str) -> dict:
        account = self._account(ref)
        uid = account["uid"]
        with self._apples_lock:
            apple = self._apples.pop(uid, None)
        if apple is not None:
            apple.logout()

        def apply(data: dict) -> None:
            (data.get("accounts") or {}).pop(uid, None)
            data["mounts"] = [m for m in data.get("mounts") or [] if m["account"] != uid]

        self.store.change(apply)
        shutil.rmtree(self.session_dir(uid), ignore_errors=True)
        self.invalidate()
        return {"logged_out": account["apple_id"]}

    # --- подключения ---------------------------------------------------------------------------------------------

    @staticmethod
    def _free_name(mounts: list[dict], wanted: str) -> str:
        names = {m["name"].casefold() for m in mounts}
        name, n = wanted, 2
        while name.casefold() in names:
            name, n = f"{wanted} ({n})", n + 1
        return name

    def mounts(self) -> list[dict]:
        accounts = self.store.load().get("accounts") or {}
        out = []
        for m in self.store.load().get("mounts") or []:
            account = accounts.get(m["account"])
            out.append({**m, "files_path": f"@icloud/{m['name']}",
                        "account": {"uid": account["uid"], "apple_id": account["apple_id"], "expired": bool(account.get("expired"))}
                        if account else None})
        return out

    def mount(self, name: str) -> dict:
        for m in self.store.load().get("mounts") or []:
            if m["name"] == name:
                return m
        raise NotFound(f"нет подключения {name}")

    def browse(self, account_ref: str, folder_id: str | None = None) -> dict:
        account = self._account(account_ref)

        def list_folders(apple: Apple) -> dict:
            node = apple.get(folder_id) if folder_id else apple.root()
            if not is_folder(node):
                raise UserError("это файл, а не папка")
            return {"folder": {"id": node["id"], "name": DRIVE_TITLE if not folder_id else node["name"]},
                    "folders": [{"id": n["id"], "name": name} for name, n in visible(apple.children(node)).items() if is_folder(n)]}

        try:
            return self.call(account["uid"], list_folders)
        except FsError as exc:
            raise UserError(str(exc)) from exc

    def add_mount(self, account_ref: str, folder_id: str | None = None, folder_path: str | None = None,
                  name: str | None = None) -> dict:
        """Весь iCloud Drive, папка по id (из browse) или по пути внутри Drive («Документы/Сканы»)."""
        account = self._account(account_ref)
        if folder_path and not folder_id:
            folder_id, folder_path = self._folder_by_path(account["uid"], folder_path)
        title = folder_path.rstrip("/").rsplit("/", 1)[-1] if folder_path else DRIVE_TITLE
        wanted = clean_name(name) if name else title

        def apply(data: dict) -> dict:
            mounts = data.setdefault("mounts", [])
            if name and any(m["name"].casefold() == wanted.casefold() for m in mounts):
                raise UserError(f"Подключение «{wanted}» уже есть — выберите другое имя")
            mount = {"name": self._free_name(mounts, wanted), "account": account["uid"], "folder_id": folder_id,
                     "folder_path": folder_path}
            mounts.append(mount)
            return mount

        mount = self.store.change(apply)
        return {**mount, "files_path": f"@icloud/{mount['name']}"}

    def _folder_by_path(self, uid: str, path: str) -> tuple[str, str]:
        parts = [p for p in path.strip("/").split("/") if p]
        if parts and parts[0] == DRIVE_TITLE:
            parts = parts[1:]
        if not parts:
            return None, None  # type: ignore[return-value]

        def find(apple: Apple) -> str:
            node = apple.root()
            for part in parts:
                child = visible(apple.children(node)).get(part)
                if child is None or not is_folder(child):
                    raise UserError(f"В iCloud Drive нет папки «{'/'.join(parts)}»")
                node = child
            return node["id"]

        try:
            return self.call(uid, find), "/".join(parts)
        except FsError as exc:
            raise UserError(str(exc)) from exc

    def rename(self, name: str, to: str) -> dict:
        to = clean_name(to)

        def apply(data: dict) -> dict:
            mounts = data.get("mounts") or []
            mount = next((m for m in mounts if m["name"] == name), None)
            if mount is None:
                raise UserError(f"Нет подключения «{name}»")
            if any(m is not mount and m["name"].casefold() == to.casefold() for m in mounts):
                raise UserError(f"Подключение «{to}» уже есть")
            mount["name"] = to
            return dict(mount)

        return self.store.change(apply)

    def unmount(self, name: str) -> dict:
        def apply(data: dict) -> None:
            before = len(data.get("mounts") or [])
            data["mounts"] = [m for m in data.get("mounts") or [] if m["name"] != name]
            if len(data["mounts"]) == before:
                raise UserError(f"Нет подключения «{name}»")

        self.store.change(apply)
        return {"unmounted": name}

    # --- пути @icloud → узлы ------------------------------------------------------------------------------------

    def invalidate(self, key: tuple | None = None) -> None:
        with self._lists_lock:
            if key is None:
                self._lists.clear()
            else:
                self._lists.pop(key, None)

    def root_node(self, mount: dict) -> dict:
        if mount.get("folder_id"):
            return self.call(mount["account"], lambda apple: apple.get(mount["folder_id"]))
        return self.call(mount["account"], lambda apple: apple.root())

    def children(self, mount: dict, folder: dict) -> dict[str, dict]:
        key = (mount["account"], folder["id"])
        with self._lists_lock:
            cached = self._lists.get(key)
            if cached and self.clock() - cached[0] < LIST_TTL_SEC:
                return cached[1]
        names = visible(self.call(mount["account"], lambda apple: apple.children(folder)))
        with self._lists_lock:
            self._lists[key] = (self.clock(), names)
        return names

    def walk(self, path: str) -> tuple[dict, dict | None, dict | None, str]:
        """Путь @icloud → (подключение, папка-родитель, узел или None, имя последней части)."""
        name, _, rest = path.partition("/")
        mount = self.mount(name)
        node = self.root_node(mount)
        if not rest:
            return mount, None, node, name
        parts = rest.split("/")
        parent = node
        for i, part in enumerate(parts):
            if not is_folder(parent):
                raise NotFound(path)
            child = self.children(mount, parent).get(part)
            if child is None:
                if i == len(parts) - 1:
                    return mount, parent, None, part
                raise NotFound(path)
            if i < len(parts) - 1:
                parent = child
            else:
                return mount, parent, child, part
        raise NotFound(path)


class ICloudFiles(FileProvider):
    """@icloud/<подключение>/… → iCloud Drive аккаунта."""

    def __init__(self, icloud: ICloud):
        self.c = icloud

    def list(self, path: str) -> list[dict]:
        if not path:
            return [entry(m["name"], "dir") for m in self.c.store.load().get("mounts") or []]
        mount, _, node, _ = self.c.walk(path)
        if node is None:
            raise NotFound(path)
        if not is_folder(node):
            raise FsError("это файл, а не папка")
        return [to_entry(name, child) for name, child in self.c.children(mount, node).items()]

    def stat(self, path: str) -> dict:
        if not path:
            return entry("", "dir")
        mount, _, node, name = self.c.walk(path)
        if node is None:
            raise NotFound(path)
        if "/" not in path:
            return entry(mount["name"], "dir")
        return to_entry(name, node)

    def read(self, path: str, offset: int = 0, length: int | None = None):
        mount, _, node, _ = self.c.walk(path)
        if node is None:
            raise NotFound(path)
        if is_folder(node):
            raise FsError("это папка")
        return self.c.call(mount["account"], lambda apple: apple.read(node, offset, length))

    def _writable(self, path: str, what: str) -> tuple[dict, dict, dict | None, str]:
        if "/" not in path:
            raise ReadOnly(f"{what}: подключения меняются на странице плагина «iCloud Drive»")
        mount, parent, node, name = self.c.walk(path)
        return mount, parent, node, name  # type: ignore[return-value]

    def _find(self, mount: dict, parent: dict, name: str) -> dict | None:
        self.c.invalidate((mount["account"], parent["id"]))
        return self.c.children(mount, parent).get(name)

    def _raw(self, uid: str, parent: dict, name: str) -> dict | None:
        """Узел по точному имени в iCloud (без суффиксов «(2)») — свежим списком."""
        self.c.invalidate((uid, parent["id"]))
        return {n["name"]: n for n in self.c.call(uid, lambda apple: apple.children(parent))}.get(name)

    def write(self, path: str, stream, overwrite: bool = True) -> dict:
        mount, parent, node, name = self._writable(path, "файл в корне @icloud")
        if node is not None and (is_folder(node) or not overwrite):
            raise Exists(path)
        uid = mount["account"]
        if node is None:
            self.c.call(uid, lambda apple: apple.upload(parent, name, stream))
        else:
            # iCloud не перезаписывает файл при загрузке, а кладёт копию «имя 2»: старый — в сторону, потом в корзину;
            # загрузка не удалась — старому возвращается имя
            aside = f".pulse-old-{secrets.token_hex(4)}-{name}"
            self.c.call(uid, lambda apple: apple.rename(node, aside))
            try:
                self.c.call(uid, lambda apple: apple.upload(parent, name, stream))
            except BaseException:
                old = self._raw(uid, parent, aside)
                if old is not None:
                    self.c.call(uid, lambda apple: apple.rename(old, name))
                self.c.invalidate((uid, parent["id"]))
                raise
            old = self._raw(uid, parent, aside)
            if old is not None:
                self.c.call(uid, lambda apple: apple.trash(old))
        saved = self._find(mount, parent, name)
        return to_entry(name, saved) if saved else entry(name, "file")

    def mkdir(self, path: str) -> dict:
        mount, parent, node, name = self._writable(path, "новая папка в корне @icloud")
        if node is not None:
            raise Exists(path)
        self.c.call(mount["account"], lambda apple: apple.mkdir(parent, name))
        created = self._find(mount, parent, name)
        return to_entry(name, created) if created else entry(name, "dir")

    def move(self, source: str, target: str) -> dict:
        if "/" not in source and "/" not in target:              # переименование подключения
            self.c.rename(source, target)
            return entry(target, "dir")
        src_mount, src_parent, node, _ = self._writable(source, "перенос подключения")
        dst_mount, dst_parent, existing, new_name = self._writable(target, "перенос в корень @icloud")
        if node is None:
            raise NotFound(source)
        if existing is not None:
            raise Exists(target)
        if src_mount["account"] != dst_mount["account"]:
            if is_folder(node):
                raise FsError("папки между аккаунтами переносите по файлам")
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
                for chunk in self.read(source):
                    spool.write(chunk)
                spool.seek(0)
                result = self.write(target, spool, overwrite=False)
            self.delete(source)
            return result
        uid = src_mount["account"]
        if dst_parent["id"] != src_parent["id"]:
            self.c.call(uid, lambda apple: apple.move(node, dst_parent))
            self.c.invalidate((uid, src_parent["id"]))
            node = self._find(dst_mount, dst_parent, node["name"]) or node
        if new_name != node["name"]:
            self.c.call(uid, lambda apple: apple.rename(node, new_name))
        self.c.invalidate((uid, src_parent["id"]))
        moved = self._find(dst_mount, dst_parent, new_name)
        return to_entry(new_name, moved) if moved else entry(new_name, "dir" if is_folder(node) else "file")

    def delete(self, path: str, recursive: bool = False) -> None:
        mount, parent, node, _ = self._writable(path, "отключить подключение")
        if node is None:
            raise NotFound(path)
        if is_folder(node) and not recursive and self.c.children(mount, node):
            raise NotEmpty("папка не пуста")
        self.c.call(mount["account"], lambda apple: apple.trash(node))
        self.c.invalidate((mount["account"], parent["id"]))
