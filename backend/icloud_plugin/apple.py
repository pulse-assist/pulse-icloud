"""iCloud одного аккаунта через pyicloud: вход (пароль Apple ID и код), продолжение сессии без пароля, iCloud Drive.

Это неофициальный веб-доступ iCloud (то же, что делает icloud.com). Остальной код плагина видит только этот модуль:
в тестах он подменяется целиком (tests/fake_apple.py).

Узел Drive — словарь {"id" (drivewsid), "docwsid", "etag", "zone", "type": "folder"|"file", "name" (с расширением),
"size", "modified", "raw"} — raw нужен pyicloud для операций над узлом.
"""

import io
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import IO

CHUNK = 256 * 1024


class AppleError(Exception):
    """Ошибка входа или доступа, понятная владельцу."""


class SessionExpired(AppleError):
    """Сессия Apple больше не действует — нужен вход заново."""


def _pyicloud():
    """Импорт по требованию: тесты обходятся без pyicloud, а ошибка установки видна в проверке подключения."""
    import pyicloud
    from pyicloud import exceptions

    return pyicloud, exceptions


def node_of(data: dict) -> dict:
    name = data.get("name") or data.get("drivewsid") or "без имени"
    if data.get("extension"):
        name = f"{name}.{data['extension']}"
    kind = (data.get("type") or "").lower()
    return {"id": data.get("drivewsid"), "docwsid": data.get("docwsid"), "etag": data.get("etag"), "zone": data.get("zone"),
            "type": "file" if kind == "file" else "folder", "name": name,
            "size": int(data["size"]) if data.get("size") not in (None, "") else None,
            "modified": data.get("dateModified") or data.get("dateChanged"), "raw": data}


class _Named(io.BufferedReader):
    """Файл для загрузки: pyicloud берёт имя в iCloud из file.name — нужно имя без пути."""

    def __init__(self, path: str, name: str):
        super().__init__(io.FileIO(path, "rb"))
        self._display = name

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._display


class Apple:
    """Один аккаунт Apple. Вызовы pyicloud — под замком: его сессия не рассчитана на параллельные запросы."""

    def __init__(self, api):
        self.api = api
        self.lock = threading.RLock()

    # --- вход ------------------------------------------------------------------------------------------------

    @classmethod
    def login(cls, apple_id: str, password: str, directory: Path) -> "Apple":
        pyicloud, exc = _pyicloud()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        try:
            api = pyicloud.PyiCloudService(apple_id, password, cookie_directory=str(directory))
        except exc.PyiCloudAccountLockedException as error:
            raise AppleError("Apple заблокировал вход в этот аккаунт: разблокируйте его на iforgot.apple.com") from error
        except exc.PyiCloudFailedLoginException as error:
            raise AppleError("Apple не принял Apple ID или пароль. Нужен обычный пароль Apple ID, пароль для приложений "
                             "не подойдёт") from error
        except exc.PyiCloudException as error:
            raise AppleError(f"Apple не пустил: {error}") from error
        if api.requires_2sa and not api.requires_2fa:
            raise AppleError("В аккаунте устаревшая двухэтапная проверка: включите двухфакторную аутентификацию "
                             "в настройках Apple ID и войдите снова")
        return cls(api)

    @classmethod
    def resume(cls, apple_id: str, directory: Path) -> "Apple":
        """Продолжить сохранённую сессию без пароля. Не вышло — SessionExpired."""
        pyicloud, exc = _pyicloud()
        if not directory.is_dir():
            raise SessionExpired("сессии нет")
        try:
            # пароль пустой, а не None: иначе pyicloud полезет в системное хранилище паролей
            api = pyicloud.PyiCloudService(apple_id, "", cookie_directory=str(directory))
        except exc.PyiCloudException as error:
            raise SessionExpired(str(error)) from error
        if api.requires_2fa or api.requires_2sa or not api.is_trusted_session:
            raise SessionExpired("Apple просит войти заново")
        return cls(api)

    @property
    def needs_code(self) -> bool:
        return bool(self.api.requires_2fa)

    @property
    def delivery(self) -> str:
        """Куда ушёл код: trusted_device (устройства Apple), sms, security_key, unknown."""
        return getattr(self.api, "two_factor_delivery_method", "unknown") or "unknown"

    def request_code(self) -> str:
        _, exc = _pyicloud()
        with self.lock:
            if getattr(self.api, "security_key_names", None):
                raise AppleError("Аккаунт защищён ключом безопасности: такой вход плагин не поддерживает")
            try:
                self.api.request_2fa_code()
            except exc.PyiCloudNoTrustedNumberAvailable as error:
                raise AppleError("Apple не нашёл доверенный номер для кода") from error
            except exc.PyiCloudAPIResponseException:
                # запрос кода не прошёл, но устройство могло уже показать код — его и примем
                self.api.use_existing_trusted_device_code()
            return self.delivery

    def request_sms(self) -> str:
        with self.lock:
            sender = getattr(self.api, "_request_sms_2fa_code", None)
            can = getattr(self.api, "_can_request_sms_2fa_code", lambda: False)
            if sender is None or not can():
                raise AppleError("Код по SMS для этого аккаунта недоступен: введите код с устройства Apple")
            sender()
            return self.delivery

    def submit_code(self, code: str) -> bool:
        _, exc = _pyicloud()
        with self.lock:
            try:
                if not self.api.validate_2fa_code(code):
                    return False
            except exc.PyiCloudException as error:
                raise AppleError(f"Apple не принял код: {error}") from error
            if not self.api.is_trusted_session:
                self.api.trust_session()
            return True

    def forget_password(self) -> None:
        """После входа пароль не нужен: дальше живёт сессия. Не держим его и в памяти."""
        if hasattr(self.api, "_password_raw"):
            self.api._password_raw = ""

    def info(self) -> dict:
        ds = (getattr(self.api, "data", None) or {}).get("dsInfo") or {}
        return {"name": ds.get("fullName") or " ".join(p for p in (ds.get("firstName"), ds.get("lastName")) if p) or None,
                "dsid": ds.get("dsid")}

    def logout(self) -> None:
        try:
            with self.lock:
                self.api.logout()
        except Exception:  # noqa: BLE001 — сессию на сервере плагина удаляем всё равно
            pass

    # --- Drive -----------------------------------------------------------------------------------------------

    def _call(self, fn, *args, **kwargs):
        _, exc = _pyicloud()
        try:
            with self.lock:
                return fn(*args, **kwargs)
        except exc.PyiCloudServiceNotActivatedException as error:
            raise AppleError("iCloud Drive не включён в этом аккаунте") from error
        except (exc.PyiCloudAuthRequiredException, exc.PyiCloud2FARequiredException) as error:
            raise SessionExpired("Apple просит войти заново") from error
        except exc.PyiCloudAPIResponseException as error:
            if getattr(error, "code", None) in (401, 421, 450):
                raise SessionExpired("Apple просит войти заново") from error
            if "PCS" in str(error) or "consent" in str(error).lower():
                raise AppleError("iCloud Drive закрыт для веба: включена «Расширенная защита данных». На iPhone: Настройки "
                                 "→ ваше имя → iCloud → «Доступ к данным iCloud через веб», затем войдите снова") from error
            raise AppleError(f"iCloud ответил ошибкой: {error}") from error
        except exc.PyiCloudServiceUnavailable as error:
            raise AppleError("iCloud Drive сейчас недоступен") from error

    def _drive(self):
        return self._call(lambda: self.api.drive)

    def _node(self, node: dict):
        from pyicloud.services.drive import DriveNode

        return DriveNode(self._drive(), dict(node["raw"]))

    def root(self) -> dict:
        drive = self._drive()
        return node_of(self._call(lambda: drive.root.data))

    def get(self, node_id: str) -> dict:
        drive = self._drive()
        return node_of(self._call(drive.get_node_data, node_id))

    def children(self, folder: dict) -> list[dict]:
        drive = self._drive()
        data = self._call(drive.get_node_data, folder["id"], (folder["raw"] or {}).get("shareID"))
        return [node_of(item) for item in data.get("items") or []]

    def read(self, node: dict, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        if not node.get("size"):
            return iter(())
        headers = {}
        if offset or length is not None:
            end = "" if length is None else str(offset + length - 1)
            headers["Range"] = f"bytes={offset}-{end}"
        response = self._call(self._node(node).open, stream=True, headers=headers)

        def stream() -> Iterator[bytes]:
            skip = offset if offset and response.status_code == 200 else 0     # сервер не понял Range — отрежем сами
            left = length
            try:
                for chunk in response.iter_content(CHUNK):
                    if skip:
                        if len(chunk) <= skip:
                            skip -= len(chunk)
                            continue
                        chunk, skip = chunk[skip:], 0
                    if left is not None:
                        chunk = chunk[:left]
                        left -= len(chunk)
                    if chunk:
                        yield chunk
                    if left == 0:
                        break
            finally:
                response.close()

        return stream()

    def upload(self, folder: dict, name: str, stream: IO[bytes]) -> None:
        with tempfile.TemporaryDirectory(prefix="icloud-up-") as tmp:
            path = os.path.join(tmp, "upload")
            with open(path, "wb") as out:
                while chunk := stream.read(CHUNK):
                    out.write(chunk)
            with _Named(path, name) as named:
                self._call(self._node(folder).upload, named)

    def mkdir(self, folder: dict, name: str) -> None:
        self._call(self._node(folder).mkdir, name)

    def rename(self, node: dict, name: str) -> None:
        """У файла в iCloud имя и расширение — отдельные поля: меняем оба."""
        drive = self._drive()
        payload = {"drivewsid": node["id"], "etag": node["etag"]}
        if node["type"] == "file" and "." in name.strip("."):
            stem, _, ext = name.rpartition(".")
            payload.update(name=stem, extension=ext)
        else:
            payload["name"] = name

        def send():
            response = drive.session.post(drive.service_root + "/renameItems", params=drive.params, json={"items": [payload]})
            drive._raise_if_error(response)
            return response.json()

        self._call(send)

    def move(self, node: dict, folder: dict) -> None:
        drive = self._drive()
        self._call(drive.move_nodes_to_node, [self._node(node)], self._node(folder))

    def trash(self, node: dict) -> None:
        self._call(self._node(node).move_to_trash)

    def storage(self) -> dict | None:
        try:
            usage = self._call(lambda: self.api.account.storage.usage)
            return {"used": int(usage.used_storage_in_bytes), "total": int(usage.total_storage_in_bytes)}
        except Exception:  # noqa: BLE001 — место на диске не обязательно
            return None
