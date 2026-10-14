"""LiveServer.write's fallback after posix_rename raises any OSError: it removes the live file, then renames the
upload; if that fails it also deletes the upload. A slow server that did the rename but answered after IO_TIMEOUT
(here 1 s instead of 60 s) ends with neither the old nor the new file on the site. Also: a server without the rename
extension whose plain rename then fails."""
import time
from pathlib import Path
from typing import Any

import paramiko
import pytest

from app.integrations import sftp
from tests.test_blog import _Files, sftp_server  # noqa: F401 - the repo's in-process SFTP server (fixture)


def _login(port: int) -> sftp.Login:
    return sftp.Login("127.0.0.1", port, "example.org", "s3cret-sftp-pass")


def test_slow_posix_rename_leaves_no_file(sftp_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    port, key, root = sftp_server
    (root / "links.html").write_bytes(b"old page")
    real = _Files.posix_rename

    def slow(self: Any, oldpath: str, newpath: str) -> Any:
        result = real(self, oldpath, newpath)  # done on the server...
        time.sleep(2.5)  # ...but answered after the client's timeout
        return result

    monkeypatch.setattr(_Files, "posix_rename", slow)
    monkeypatch.setattr(sftp, "IO_TIMEOUT", 1)
    server = sftp.connect(_login(port), sftp.fingerprint(key.asbytes()))
    try:
        with pytest.raises(sftp.SftpError) as raised:
            server.write("links.html", b"new page")
    finally:
        server.close()
    time.sleep(3)  # let the server finish what it was sent
    left = sorted(p.name for p in root.iterdir())
    print("\nerror:", type(raised.value).__name__, raised.value)
    print("files on the server:", left, "| links.html:", (root / "links.html").read_bytes() if (root / "links.html").exists() else None)


def test_no_rename_extension_and_a_failing_rename(sftp_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    port, key, root = sftp_server
    (root / "links.html").write_bytes(b"old page")
    monkeypatch.setattr(_Files, "posix_rename", lambda self, a, b: paramiko.SFTP_OP_UNSUPPORTED)
    monkeypatch.setattr(_Files, "rename", lambda self, a, b: paramiko.SFTP_PERMISSION_DENIED)
    server = sftp.connect(_login(port), sftp.fingerprint(key.asbytes()))
    try:
        with pytest.raises(sftp.SftpError) as raised:
            server.write("links.html", b"new page")
    finally:
        server.close()
    print("\nerror:", type(raised.value).__name__, raised.value)
    print("files on the server:", sorted(p.name for p in root.iterdir()))
