"""Плагин «iCloud Drive»: аккаунты Apple (вход паролем и кодом на странице), подключения, хранилище @icloud для
«Файлов» и агентов.

Страница (pulse.plugin): GET /state, POST|DELETE /login, POST /login/code, POST /login/sms, DELETE /accounts/{uid},
GET /browse, POST /mounts, PATCH|DELETE /mounts/{name}, GET /check.
Агенты: pulse icloud accounts|login|login code|login status|logout|browse|mounts|mount|rename|unmount|check.
Хранилище для ядра — /fs/… (pulse_plugin.fs). Пароль Apple ID принимает только страница: у агентов его нет.
"""

import httpx
from fastapi import Body, HTTPException, Query
from pulse_plugin import CommandError, Plugin
from pulse_plugin.fs import FsError

from .service import ICloud, ICloudFiles, UserError
from .store import Store

plugin = Plugin()
app = plugin.app


def expired_signal(account: dict) -> None:
    """Сессия истекла — один сигнал на аккаунт, пока владелец не войдёт снова."""
    try:
        plugin.signal(f"Войдите в iCloud снова: {account['apple_id']}",
                      body=f"Вход в аккаунт Apple {account['apple_id']} истёк — его подключения в @icloud не открываются. "
                           "Откройте [iCloud Drive](/p/icloud/main) и войдите ещё раз: Apple ID, пароль и код.",
                      priority=70, tags=["icloud"])
    except Exception:  # noqa: BLE001 — сигнал не обязателен: страница всё равно покажет, что вход истёк
        pass


icloud = ICloud(Store(plugin.data_dir), plugin.data_dir / "sessions", on_expired=expired_signal)
files = ICloudFiles(icloud)
plugin.provide_files(files)


def run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FsError as exc:
        raise HTTPException(exc.status if exc.status < 500 else 502, str(exc)) from exc


def command(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (UserError, FsError) as exc:
        raise CommandError(str(exc)) from exc


# --- страница ---------------------------------------------------------------------------------------------------

@app.get("/state")
def state(space: bool = Query(True)) -> dict:
    return {"accounts": icloud.accounts(space=space), "mounts": icloud.mounts(), "login": icloud.login_state()}


@app.post("/login")
def login(body: dict = Body(...)) -> dict:
    """Apple ID и пароль — только отсюда, со страницы владельца; пароль дальше pyicloud не уходит и не сохраняется."""
    return run(icloud.start_login, body.get("apple_id") or "", body.get("password") or "")


@app.post("/login/code")
def login_code(body: dict = Body(...)) -> dict:
    return run(icloud.submit_code, str(body.get("code") or ""))


@app.post("/login/sms")
def login_sms() -> dict:
    return run(icloud.send_sms)


@app.delete("/login")
def cancel_login() -> dict:
    icloud.cancel_login()
    return {"login": None}


@app.delete("/accounts/{uid}")
def logout(uid: str) -> dict:
    return run(icloud.logout, uid)


@app.get("/browse")
def browse(account: str = Query(...), folder: str | None = Query(None)) -> dict:
    return run(icloud.browse, account, folder)


@app.post("/mounts")
def add_mount(body: dict = Body(...)) -> dict:
    return run(icloud.add_mount, body.get("account") or "", body.get("folder_id") or None, body.get("folder_path") or None,
               body.get("name") or None)


@app.patch("/mounts/{name}")
def rename_mount(name: str, body: dict = Body(...)) -> dict:
    return run(icloud.rename, name, body.get("name") or "")


@app.delete("/mounts/{name}")
def remove_mount(name: str) -> dict:
    return run(icloud.unmount, name)


# --- проверка подключения ------------------------------------------------------------------------------------------

REACH = [("Вход Apple: idmsa.apple.com", "https://idmsa.apple.com/appleauth/auth"), ("iCloud: www.icloud.com", "https://www.icloud.com")]


def connection_check() -> dict:
    checks = []

    def add(title: str, ok: bool, detail: str | None = None) -> None:
        checks.append({"title": title, "ok": ok, "detail": detail})

    try:
        import pyicloud  # noqa: F401
        from importlib.metadata import version
        add("Библиотека pyicloud", True, f"версия {version('pyicloud')}")
    except Exception as exc:  # noqa: BLE001
        add("Библиотека pyicloud", False, f"не загружается: {exc} — переустановите плагин")
    for title, url in REACH:
        try:
            response = httpx.get(url, timeout=10, follow_redirects=False)
            add(title, True, f"отвечает ({response.status_code})")
        except httpx.HTTPError as exc:
            add(title, False, f"не отвечает: {type(exc).__name__}. Сервер Пульса не видит Apple — проверьте сеть")
    for account in icloud.accounts():
        title = f"Аккаунт {account['apple_id']}"
        if account["expired"]:
            add(title, False, "вход истёк — войдите снова на странице плагина")
            continue
        try:
            icloud.call(account["uid"], lambda apple: apple.root())
            add(title, True, "вход действует, iCloud Drive открывается")
        except FsError as exc:
            add(title, False, str(exc))
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@app.get("/check")
def check_page() -> dict:
    return connection_check()


# --- команды агентов ----------------------------------------------------------------------------------------------

TELL_OWNER = ("Откройте [iCloud Drive](/p/icloud/main) → «Войти в аккаунт Apple»: введите Apple ID и пароль, затем код "
              "с устройства Apple. Пароль вводится только на странице — в чат его не присылайте.")


@plugin.command("accounts", "Аккаунты Apple: Apple ID, место в iCloud, не истёк ли вход")
def accounts_command(args: dict) -> dict:
    return {"accounts": icloud.accounts(space=True)}


@plugin.command("login", "Как войти в аккаунт Apple: пароль и код вводит владелец на странице плагина")
def login_command(args: dict) -> dict:
    return {"login": icloud.login_state(), "tell_owner": TELL_OWNER}


@plugin.command("login code", "Ввести код подтверждения Apple, который владелец прислал в чат (вход уже начат на странице)")
def login_code_command(args: dict) -> dict:
    if not args.get("code"):
        raise CommandError("нужен --code — 6 цифр")
    return command(icloud.submit_code, str(args["code"]))


@plugin.command("login status", "Чем закончился вход: code (ждёт код), done, error, expired")
def login_status_command(args: dict) -> dict:
    return icloud.login_state() or {"status": "none"}


@plugin.command("logout", "Выйти из аккаунта Apple и убрать его подключения (файлы в iCloud не трогаются)")
def logout_command(args: dict) -> dict:
    return command(icloud.logout, args.get("account") or "")


@plugin.command("browse", "Папки iCloud Drive: в корне или внутри папки — чтобы выбрать, что подключить")
def browse_command(args: dict) -> dict:
    return command(icloud.browse, args.get("account") or "", args.get("folder") or None)


@plugin.command("mounts", "Подключения: папки в @icloud и откуда они")
def mounts_command(args: dict) -> dict:
    return {"mounts": icloud.mounts()}


@plugin.command("mount", "Подключить весь iCloud Drive или папку в нём (по id из browse или по пути)")
def mount_command(args: dict) -> dict:
    return command(icloud.add_mount, args.get("account") or "", args.get("folder") or None, args.get("path") or None,
                   args.get("name") or None)


@plugin.command("rename", "Переименовать подключение")
def rename_command(args: dict) -> dict:
    if not args.get("name") or not args.get("to"):
        raise CommandError("нужны --name и --to")
    return command(icloud.rename, args["name"], args["to"])


@plugin.command("unmount", "Отключить подключение (файлы в iCloud не трогаются)")
def unmount_command(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name")
    return command(icloud.unmount, args["name"])


@plugin.command("check", "Проверить подключение: библиотека, доступ к Apple, аккаунты")
def check_command(args: dict) -> dict:
    return connection_check()
