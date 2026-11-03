"""Плагин без сети: iCloud подменён (fake_apple.py). Запуск: cd backend && pytest"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PULSE_PLUGIN_DATA", tempfile.mkdtemp(prefix="icloud-test-"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fake_apple import CODE, PASSWORD, FakeCloud, factories  # noqa: E402
from icloud_plugin import app as module  # noqa: E402
from icloud_plugin.service import ICloud, uid_of, visible  # noqa: E402
from icloud_plugin.store import Store  # noqa: E402

APPLE_ID = "artem@icloud.com"


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    cloud = FakeCloud()
    login, resume = factories(cloud)
    signals = []
    service = ICloud(Store(tmp_path), tmp_path / "sessions", login=login, resume=resume, on_expired=signals.append)
    monkeypatch.setattr(module, "icloud", service)
    monkeypatch.setattr(module.files, "c", service)
    cloud.service, cloud.signals, cloud.dir = service, signals, tmp_path
    return cloud


@pytest.fixture
def client(cloud):
    return TestClient(module.app)


def login(client, apple_id=APPLE_ID):
    started = client.post("/login", json={"apple_id": apple_id, "password": PASSWORD})
    assert started.status_code == 200, started.text
    assert started.json() == {"status": "code", "apple_id": apple_id, "delivery": "trusted_device"}
    done = client.post("/login/code", json={"code": CODE})
    assert done.status_code == 200, done.text
    return done.json()


def items(client, path):
    response = client.get("/fs/list", params={"path": path})
    assert response.status_code == 200, response.text
    return {i["name"]: i for i in response.json()["items"]}


def test_login_with_code_connects_whole_drive_and_keeps_no_password(client, cloud):
    assert login(client) == {"status": "done", "apple_id": APPLE_ID}
    state = client.get("/state").json()
    assert state["accounts"] == [{"uid": uid_of(APPLE_ID), "apple_id": APPLE_ID, "name": "Artem D.", "expired": False,
                                  "storage": {"used": 5 * 1024**3, "total": 50 * 1024**3}}]
    assert [(m["name"], m["files_path"]) for m in state["mounts"]] == [("iCloud Drive", "@icloud/iCloud Drive")]
    assert PASSWORD not in json.dumps(state)
    saved = (cloud.dir / "state.json").read_text(encoding="utf-8")
    assert PASSWORD not in saved and "password" not in saved
    apple = cloud.service._apples[uid_of(APPLE_ID)]
    assert apple.password_forgotten


def test_wrong_password_wrong_code_and_sms(client, cloud):
    bad = client.post("/login", json={"apple_id": APPLE_ID, "password": "nope"})
    assert bad.status_code == 400 and "не принял" in bad.json()["detail"]
    assert client.get("/state").json()["login"]["status"] == "error"
    assert client.post("/login", json={"apple_id": "не почта", "password": PASSWORD}).status_code == 400
    assert client.post("/login/code", json={"code": CODE}).status_code == 400          # вход не начат

    client.post("/login", json={"apple_id": APPLE_ID, "password": PASSWORD})
    assert client.post("/login/code", json={"code": "12345"}).json()["detail"] == "Код — 6 цифр"
    assert client.post("/login/code", json={"code": "654321"}).json()["detail"].startswith("Код не подошёл")
    assert client.post("/login/sms").json()["delivery"] == "sms"
    assert client.post("/login/code", json={"code": "123 456"}).json()["status"] == "done"


def test_pending_login_expires_and_cancel(client, cloud):
    now = [1000.0]
    cloud.service.clock = lambda: now[0]
    client.post("/login", json={"apple_id": APPLE_ID, "password": PASSWORD})
    now[0] += 11 * 60
    assert client.get("/state").json()["login"]["status"] == "expired"
    assert client.post("/login/code", json={"code": CODE}).status_code == 400
    client.post("/login", json={"apple_id": APPLE_ID, "password": PASSWORD})
    client.delete("/login")
    assert client.get("/state").json()["login"] is None


def test_trusted_session_needs_no_code(client, cloud):
    cloud.needs_code = False
    started = client.post("/login", json={"apple_id": APPLE_ID, "password": PASSWORD}).json()
    assert started["status"] == "done"


def test_advanced_data_protection_is_explained(client, cloud):
    cloud.adp.add(APPLE_ID)
    client.post("/login", json={"apple_id": APPLE_ID, "password": PASSWORD})
    answer = client.post("/login/code", json={"code": CODE})
    assert answer.status_code == 400 and "Расширенная защита данных" in answer.json()["detail"]
    assert client.get("/state").json()["accounts"] == []


def test_files_list_read_and_duplicates(client, cloud):
    login(client)
    top = items(client, "iCloud Drive")
    assert set(top) == {"Документы", "Отчёт.pages"} and top["Документы"]["kind"] == "dir"
    docs = items(client, "iCloud Drive/Документы")
    assert set(docs) == {"Сканы", "план.txt", "план (2).txt"}
    assert client.get("/fs/read", params={"path": "iCloud Drive/Документы/план (2).txt"}).content == b"second"
    part = client.get("/fs/read", params={"path": "iCloud Drive/Документы/план.txt"}, headers={"Range": "bytes=0-7"})
    assert part.content == "план на неделю".encode()[:8]
    assert client.get("/fs/read", params={"path": "iCloud Drive/нет.txt"}).status_code == 404


def test_write_new_file_mkdir_move_and_trash(client, cloud):
    login(client)
    assert client.put("/fs/write", params={"path": "iCloud Drive/Документы/итоги.md"}, content=b"# done").status_code == 200
    assert client.get("/fs/read", params={"path": "iCloud Drive/Документы/итоги.md"}).content == b"# done"
    assert client.post("/fs/mkdir", json={"path": "iCloud Drive/Архив"}).status_code == 200
    moved = client.post("/fs/move", json={"source": "iCloud Drive/Документы/итоги.md", "target": "iCloud Drive/Архив/итоги 2026.md"})
    assert moved.status_code == 200, moved.text
    assert set(items(client, "iCloud Drive/Архив")) == {"итоги 2026.md"}
    assert "итоги.md" not in items(client, "iCloud Drive/Документы")
    assert client.delete("/fs/delete", params={"path": "iCloud Drive/Документы"}).status_code == 409          # не пуста
    assert client.delete("/fs/delete", params={"path": "iCloud Drive/Архив/итоги 2026.md"}).status_code in (200, 204)
    assert ("trash" in [c[0] for c in cloud.calls])
    assert items(client, "iCloud Drive/Архив") == {}


def test_overwrite_replaces_file_instead_of_copy(client, cloud):
    login(client)
    path = "iCloud Drive/Документы/Сканы/паспорт.pdf"
    assert client.put("/fs/write", params={"path": path}, content=b"%PDF-new").status_code == 200
    names = set(items(client, "iCloud Drive/Документы/Сканы"))
    assert names == {"паспорт.pdf"}                                  # ни «паспорт 2.pdf», ни служебной копии
    assert client.get("/fs/read", params={"path": path}).content == b"%PDF-new"
    assert client.put("/fs/write", params={"path": path, "overwrite": "false"}, content=b"x").status_code == 409


def test_failed_overwrite_brings_old_file_back(client, cloud):
    login(client)
    client.put("/fs/write", params={"path": "iCloud Drive/сбой.txt"}, content=b"")      # такой файл не загрузить
    drive = cloud.drives[APPLE_ID]
    first = cloud.add(APPLE_ID, "root", "сбой.txt", b"old")
    cloud.service.invalidate()
    answer = client.put("/fs/write", params={"path": "iCloud Drive/сбой.txt"}, content=b"new")
    assert answer.status_code >= 400
    assert drive[first]["name"] == "сбой.txt" and drive[first]["parent"] == "root"
    assert client.get("/fs/read", params={"path": "iCloud Drive/сбой.txt"}).content == b"old"


def test_root_of_icloud_is_read_only(client, cloud):
    login(client)
    assert client.put("/fs/write", params={"path": "файл.txt"}, content=b"x").status_code == 403
    assert client.post("/fs/mkdir", json={"path": "Новая"}).status_code == 403
    assert set(items(client, "")) == {"iCloud Drive"}


def test_expired_session_marks_account_and_signals_once(client, cloud):
    login(client)
    cloud.revoked.add(APPLE_ID)
    cloud.service.invalidate()
    assert client.get("/fs/list", params={"path": "iCloud Drive"}).status_code >= 400
    assert client.get("/fs/list", params={"path": "iCloud Drive"}).status_code >= 400
    assert len(cloud.signals) == 1 and cloud.signals[0]["apple_id"] == APPLE_ID
    assert client.get("/state", params={"space": "false"}).json()["accounts"][0]["expired"] is True
    # вход заново тем же Apple ID — подключения снова открываются
    cloud.revoked.clear()
    login(client)
    assert client.get("/state").json()["accounts"][0]["expired"] is False
    assert set(items(client, "iCloud Drive")) == {"Документы", "Отчёт.pages"}


def test_session_resumes_after_plugin_restart(client, cloud, tmp_path):
    login(client)
    cloud.service._apples.clear()                                     # процесс плагина перезапустился
    assert set(items(client, "iCloud Drive")) == {"Документы", "Отчёт.pages"}


def test_folder_mount_by_path_and_by_browse(client, cloud):
    login(client)
    by_path = client.post("/mounts", json={"account": APPLE_ID, "folder_path": "iCloud Drive/Документы/Сканы"}).json()
    assert by_path["name"] == "Сканы" and by_path["files_path"] == "@icloud/Сканы"
    assert set(items(client, "Сканы")) == {"паспорт.pdf"}
    top = client.get("/browse", params={"account": APPLE_ID}).json()
    assert [f["name"] for f in top["folders"]] == ["Документы"] and top["folder"]["name"] == "iCloud Drive"
    inner = client.get("/browse", params={"account": APPLE_ID, "folder": top["folders"][0]["id"]}).json()
    named = client.post("/mounts", json={"account": APPLE_ID, "folder_id": inner["folders"][0]["id"], "folder_path": "Документы/Сканы",
                                         "name": "Сканы паспорта"}).json()
    assert named["name"] == "Сканы паспорта"
    assert client.post("/mounts", json={"account": APPLE_ID, "folder_path": "Нет такой"}).status_code == 400
    assert client.patch("/mounts/Сканы", json={"name": "Сканы паспорта"}).status_code == 400
    assert client.patch("/mounts/Сканы", json={"name": "Сканы-2"}).json()["name"] == "Сканы-2"
    assert client.delete("/mounts/Сканы-2").status_code == 200
    assert [m["name"] for m in client.get("/state").json()["mounts"]] == ["iCloud Drive", "Сканы паспорта"]


def test_logout_removes_session_and_mounts(client, cloud):
    login(client)
    session = cloud.dir / "sessions" / uid_of(APPLE_ID)
    assert session.is_dir()
    assert client.delete(f"/accounts/{uid_of(APPLE_ID)}").json() == {"logged_out": APPLE_ID}
    state = client.get("/state").json()
    assert state["accounts"] == [] and state["mounts"] == [] and not session.exists()


def test_second_account_gets_own_mount_name(client, cloud):
    login(client)
    login(client, "work@company.com")
    assert [m["name"] for m in client.get("/state").json()["mounts"]] == ["iCloud Drive", "iCloud Drive (work@company.com)"]


def test_agent_commands_never_take_password(cloud):
    commands = json.loads((Path(__file__).parents[2] / "commands.json").read_text(encoding="utf-8"))
    for command in commands:
        assert all("pass" not in a["name"] for a in command.get("args", []))
    registered = {c["name"] for c in commands}
    assert registered == {"accounts", "login", "login code", "login status", "logout", "browse", "mounts", "mount", "rename", "unmount", "check"}


def test_visible_hides_replacement_leftovers():
    names = visible([{"name": ".pulse-old-ab12-план.txt", "type": "file"}, {"name": "план.txt", "type": "file"}])
    assert list(names) == ["план.txt"]
