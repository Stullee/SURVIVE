"""SFTP to the owner's web host (0.14.0): the only way Ember's code writes to their website.

Only Ember's code connects, never the agent (its tools have no network): the publisher (site_publisher.py) uploads
exactly the pages the owner approved, and (0.16.0) the live view's files (live_view.py). The login is the owner's
(their options: blog_sftp_*); the password is never logged or shown. The server must show the key the owner pinned
(blog_sftp_host_key) or, without one, the key Ember's code saw at its first connection (trust on first use, kept per
host): a server with another key gets nothing, so the login and the pages can only ever reach the owner's server.

``Server`` is what the publisher uses: read a file, write one (to a temporary name first, then renamed over the old
one, and read back), remove one. Every path is checked against blog.allowed (a post, the blog's list, the link page)
or blog.LIVE_FILES before anything is sent. ``FakeServer`` stands in for it in a dry run: nothing leaves the app.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import logging
import posixpath
import re
import secrets
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..products import blog

logging.getLogger("paramiko").setLevel(logging.WARNING)  # its transport logs every packet at DEBUG

TIMEOUT = 20  # seconds: connecting, the SSH handshake and the login
IO_TIMEOUT = 60  # seconds: one read or write
# One folder's name. 0.16.1 checked the whole path with one pattern whose repeated group backtracked exponentially on
# a character outside it (30 characters ending in an umlaut took 17 seconds, holding the whole app): each name alone.
SEGMENT = re.compile(r"[A-Za-z0-9._~ -]+")
_PIN = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


class SftpError(Exception):
    """Something the server or the connection did, in words for the owner (never holding the password)."""


class NotSent(SftpError):
    """Nothing changed on the server (it can't be reached, it refused the login, its key isn't the pinned one, or a
    page couldn't be read)."""


class HostKeyMismatch(NotSent):
    """The server showed another key than the pinned one: nothing was sent, not even the login."""


class Unclear(SftpError):
    """It can't be known whether the server has the new file."""


@dataclass(frozen=True)
class Login:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    folder: str = ""  # the website's folder on the server ("": the folder the login opens)
    host_key: str = ""  # the owner's pin ("": the first key seen, kept)

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}"


def fingerprint(blob: bytes) -> str:
    """A server key's fingerprint as OpenSSH prints it (ssh-keygen -lf): SHA256: and base64 without padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def pin_of(text: str) -> str:
    """The owner's pin as a fingerprint: 'SHA256:...' as SSH tools print it, or a public key line
    ('ssh-ed25519 AAAA...', as ssh-keyscan prints it, with or without the host first). Raises ValueError."""
    value = " ".join(text.split())
    if _PIN.match(value):
        return value.rstrip("=")
    parts = value.split(" ")
    for i, part in enumerate(parts[:-1]):
        if part.startswith(("ssh-", "ecdsa-", "rsa-")):
            try:
                return fingerprint(base64.b64decode(parts[i + 1], validate=True))
            except (binascii.Error, ValueError):
                break
    raise ValueError(
        "blog_sftp_host_key must be the server's key fingerprint (SHA256:..., 43 characters after the colon) or its "
        "public key line (ssh-ed25519 AAAA...)"
    )


def folder_of(text: str) -> str:
    """The website's folder on the server, checked: names separated by single slashes (one at the start and the end
    may stand), no '..', no odd characters. Raises ValueError."""
    value = text.strip()
    inner = value.removeprefix("/")
    names = inner.removesuffix("/").split("/") if inner else []
    if not all(SEGMENT.fullmatch(name) and name != ".." for name in names):
        raise ValueError("blog_sftp_folder is a folder on your server like /ember-ai.de or empty")
    return value.rstrip("/") if value not in ("", "/") else value


class Server(Protocol):
    simulated: bool
    fingerprint: str

    def read(self, path: str) -> bytes | None: ...

    def write(self, path: str, data: bytes) -> None: ...

    def remove(self, path: str) -> None: ...

    def close(self) -> None: ...


def _checked(path: str) -> str:
    if not blog.allowed(path) and path not in blog.LIVE_FILES:
        raise NotSent(
            f"{path} isn't a file Ember's code may write (a post, the blog's list, the link page or the live view)"
        )
    return path


class FakeServer:
    """The dry run's server: the files live in this object (kept for the app's run), nothing leaves the app."""

    simulated = True
    fingerprint = "SHA256:dry-run-fake-server"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def read(self, path: str) -> bytes | None:
        with self._lock:
            return self.files.get(_checked(path))

    def write(self, path: str, data: bytes) -> None:
        with self._lock:
            self.files[_checked(path)] = bytes(data)

    def remove(self, path: str) -> None:
        with self._lock:
            self.files.pop(_checked(path), None)

    def close(self) -> None:
        return None


class LiveServer:
    """The owner's server, logged in over SFTP (paramiko). One connection serves one run of the publisher."""

    simulated = False

    def __init__(self, transport: Any, client: Any, folder: str, seen: str) -> None:
        import paramiko  # loaded already: a LiveServer is made only by connect()

        self._transport = transport
        self._sftp = client
        self._folder = folder
        self.fingerprint = seen
        # What a broken connection raises. 0.16.1: paramiko's SSHException too ("Server connection dropped", when the
        # connection drops during a transfer), which escaped: the live view logged in again every round, with no
        # back-off, and the owner's check answered 500.
        self._dropped: type[BaseException] = paramiko.SSHException
        self._broken: tuple[type[BaseException], ...] = (OSError, EOFError, self._dropped)

    def _full(self, path: str) -> str:
        _checked(path)
        return posixpath.join(self._folder, path) if self._folder else path

    def read(self, path: str) -> bytes | None:
        full = self._full(path)
        try:
            with self._sftp.open(full, "rb") as handle:
                data = handle.read(blog.PAGE_MAX + 1)
        except FileNotFoundError:
            return None
        except self._broken as exc:
            raise NotSent(f"{path} on the server can't be read ({_why(exc)})") from None
        if len(data) > blog.PAGE_MAX:
            raise NotSent(f"{path} on the server is larger than {blog.PAGE_MAX // 1000} kB")
        return bytes(data)

    def _folder_for(self, full: str) -> None:
        parent = posixpath.dirname(full)
        if not parent or parent == "/":
            return
        try:
            self._sftp.stat(parent)
        except FileNotFoundError:
            try:
                self._sftp.mkdir(parent, 0o755)
            except self._broken as exc:
                raise NotSent(f"the folder {posixpath.basename(parent)} can't be made ({_why(exc)})") from None
        except self._broken as exc:
            raise NotSent(f"the server can't be read ({_why(exc)})") from None

    def write(self, path: str, data: bytes) -> None:
        """Upload under a temporary name, rename it over the old file, read it back. NotSent: the old file is
        untouched; Unclear: it may or may not be the new one."""
        full = self._full(path)
        self._folder_for(full)
        temporary = posixpath.join(posixpath.dirname(full), f".ember-{secrets.token_hex(6)}.tmp")
        try:
            with self._sftp.open(temporary, "wb") as handle:
                handle.write(data)
        except self._broken as exc:
            self._forget(temporary)
            raise NotSent(f"{path} couldn't be uploaded ({_why(exc)})") from None
        try:
            try:
                self._sftp.posix_rename(temporary, full)
            except OSError:  # a server without OpenSSH's rename extension: the old file goes first
                with contextlib.suppress(FileNotFoundError):
                    self._sftp.remove(full)
                self._sftp.rename(temporary, full)
        except self._broken as exc:
            self._forget(temporary)
            raise Unclear(f"{path} was uploaded but couldn't be put in place ({_why(exc)})") from None
        try:
            back = self.read(path)
        except NotSent as exc:
            raise Unclear(f"{path} was uploaded but couldn't be read back ({exc})") from None
        if back is None or blog.sha256(back) != blog.sha256(data):
            raise Unclear(f"{path} on the server isn't the file Ember's code uploaded")

    def _forget(self, temporary: str) -> None:
        with contextlib.suppress(*self._broken):
            self._sftp.remove(temporary)

    def remove(self, path: str) -> None:
        """NotSent: the server refused it, the file is still there; Unclear: the connection dropped, it may be gone."""
        full = self._full(path)
        try:
            self._sftp.remove(full)
        except FileNotFoundError:
            return
        except self._dropped as exc:  # the request may have reached the server before the connection dropped
            raise Unclear(f"it is unclear whether {path} was removed ({_why(exc)})") from None
        except (OSError, EOFError) as exc:
            raise NotSent(f"{path} couldn't be removed ({_why(exc)})") from None

    def close(self) -> None:
        for part in (self._sftp, self._transport):
            with contextlib.suppress(Exception):  # closing a broken connection must not raise
                part.close()


def _why(exc: BaseException) -> str:
    """An error in words without the login (paramiko's messages never hold the password, but keep them short)."""
    text = " ".join(str(exc).split())[:200]
    return text or type(exc).__name__


def connect(login: Login, pinned: str | None) -> LiveServer:
    """Log in to the owner's server. With ``pinned``, a server showing another key gets nothing (HostKeyMismatch,
    before the login is sent). Raises NotSent."""
    import paramiko  # only Ember's code connects, and only live: the agent's sealed threads never load it

    try:
        sock = socket.create_connection((login.host, login.port), timeout=TIMEOUT)
    except OSError as exc:
        raise NotSent(f"{login.where} can't be reached ({_why(exc)})") from None
    transport = paramiko.Transport(sock)
    transport.banner_timeout = TIMEOUT
    transport.handshake_timeout = TIMEOUT
    transport.auth_timeout = TIMEOUT
    try:
        transport.start_client(timeout=TIMEOUT)
        seen = fingerprint(transport.get_remote_server_key().asbytes())
        if pinned and seen != pinned:
            raise HostKeyMismatch(
                f"{login.host} showed the key {seen}, not the pinned {pinned}: nothing was sent. If your host changed "
                "its key, set blog_sftp_host_key to the new fingerprint"
            )
        transport.auth_password(login.user, login.password)
        if not transport.is_authenticated():
            raise NotSent(f"{login.host} refused the login (check blog_sftp_user and blog_sftp_password)")
        client = paramiko.SFTPClient.from_transport(transport)
        if client is None:
            raise NotSent(f"{login.host} has no SFTP")
        client.get_channel().settimeout(IO_TIMEOUT)
    except HostKeyMismatch:
        transport.close()
        raise
    except paramiko.AuthenticationException:
        transport.close()
        raise NotSent(f"{login.host} refused the login (check blog_sftp_user and blog_sftp_password)") from None
    except (paramiko.SSHException, OSError, EOFError) as exc:
        transport.close()
        raise NotSent(f"no SFTP connection to {login.where} ({_why(exc)})") from None
    except NotSent:
        transport.close()
        raise
    return LiveServer(transport, client, login.folder, seen)
