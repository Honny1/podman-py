"""Specialized Transport Adapter for remote Podman access via ssh tunnel.

See Podman go bindings for more details.
"""

import collections
import functools
import logging
import pathlib
import random
import select
import socket
import subprocess
import threading
import urllib.parse
from contextlib import suppress
from typing import Optional, Union

import time

import urllib3
import urllib3.connection

from requests.adapters import DEFAULT_POOLBLOCK, DEFAULT_RETRIES, HTTPAdapter
from podman.api.path_utils import get_runtime_dir

from .adapter_utils import _key_normalizer

logger = logging.getLogger("podman.ssh_adapter")


def _identity_args(identity: Optional[str]) -> list[str]:
    if identity is None:
        return []
    path = pathlib.Path(identity).expanduser()
    return ["-i", str(path)]


def _runtime_forward_path() -> pathlib.Path:
    runtime_dir = pathlib.Path(get_runtime_dir()) / "podman"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return runtime_dir / f"podman-forward-{random.getrandbits(80):x}.sock"


class SSHSocket(socket.socket):
    """AF_UNIX socket connected through an ssh -L stream-local forward."""

    def __init__(self, uri: str, identity: Optional[str] = None):
        super().__init__(socket.AF_UNIX, socket.SOCK_STREAM)
        self.uri = uri
        self.identity = identity
        self._proc: Optional[subprocess.Popen] = None
        self.local_sock = _runtime_forward_path()

    def connect(self, **kwargs):  # pylint: disable=unused-argument
        """Connect via ssh stream-local forwarding.

        Raises:
            OSError: when the SSH channel cannot be established.
            subprocess.TimeoutExpired: when SSH client fails to create local socket.
        """
        uri = urllib.parse.urlparse(self.uri)
        command = [
            "ssh",
            "-N",
            "-o",
            "StrictHostKeyChecking no",
            "-L",
            f"{self.local_sock}:{uri.path}",
            *_identity_args(self.identity),
            f"ssh://{uri.netloc}",
        ]
        cmd = " ".join(command)
        expiration = time.monotonic() + 30

        while time.monotonic() < expiration:
            if self._proc is None or self._proc.poll() is not None:
                with suppress(FileNotFoundError):
                    self.local_sock.unlink()
                self._proc = subprocess.Popen(  # pylint: disable=consider-using-with
                    command,
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )

            while not self.local_sock.exists():
                if time.monotonic() > expiration:
                    raise subprocess.TimeoutExpired(cmd, expiration)
                if self._proc.poll() is not None:
                    break
                logger.debug("Waiting on %s", self.local_sock)
                time.sleep(0.2)
            else:
                try:
                    super().connect(str(self.local_sock))
                except OSError as exc:
                    logger.debug("Forward socket not ready: %s", exc)
                    time.sleep(0.2)
                    continue

                # Socket connected — verify the remote channel actually works.
                time.sleep(0.1)
                if self._forward_channel_failed():
                    raise OSError("SSH stream-local forward channel refused")
                return

            time.sleep(0.2)

        raise subprocess.TimeoutExpired(cmd, expiration)

    def _forward_channel_failed(self) -> bool:
        if not self._proc or not self._proc.stderr:
            return False
        while True:
            ready, _, _ = select.select([self._proc.stderr], [], [], 0.0)
            if not ready:
                break
            line = self._proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            if "open failed" in text.lower():
                logger.debug("SSH forward stderr: %s", text.strip())
                return True
        return False

    def close(self):
        if self._proc:
            if self._proc.stderr:
                with suppress(OSError):
                    self._proc.stderr.close()
            self._proc.terminate()
            try:
                self._proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

        with suppress(FileNotFoundError):
            self.local_sock.unlink()
        with suppress(OSError):
            super().close()


class _DialStdioBridge:
    """Relay between a connected socketpair and ssh dial-stdio subprocess pipes."""

    def __init__(self, uri: str, identity: Optional[str] = None):
        parsed = urllib.parse.urlparse(uri)
        command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking no",
            "-o",
            "BatchMode=yes",
            "-T",
            *_identity_args(identity),
            f"ssh://{parsed.netloc}",
            "podman",
            f"--url=unix://{parsed.path}",
            "system",
            "dial-stdio",
        ]
        self._proc = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.3)
        if self._proc.poll() is not None:
            stderr = ""
            if self._proc.stderr:
                stderr = self._proc.stderr.read().decode(errors="replace")
            raise OSError(f"SSH dial-stdio exited early: {stderr.strip()}")

        self._stop = threading.Event()
        self._client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread = threading.Thread(
            target=self._relay,
            args=(server,),
            daemon=True,
            name="podman-ssh-stdio-bridge",
        )
        self._thread.start()

    def _relay(self, server: socket.socket) -> None:
        stdin = self._proc.stdin
        stdout = self._proc.stdout
        try:
            while not self._stop.is_set():
                if self._proc.poll() is not None:
                    break
                readable, _, _ = select.select([server, stdout], [], [], 0.5)
                if server in readable:
                    chunk = server.recv(65536)
                    if not chunk:
                        break
                    stdin.write(chunk)
                    stdin.flush()
                if stdout in readable:
                    chunk = stdout.read1(65536) if hasattr(stdout, 'read1') else stdout.read(65536)
                    if not chunk:
                        break
                    server.sendall(chunk)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                server.close()

    @property
    def client_socket(self) -> socket.socket:
        return self._client

    def close(self) -> None:
        self._stop.set()
        with suppress(OSError):
            self._client.close()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

        with suppress(BrokenPipeError, OSError):
            if self._proc.stdin:
                self._proc.stdin.close()
        if self._proc.stdout:
            with suppress(OSError):
                self._proc.stdout.close()
        if self._proc.stderr:
            with suppress(OSError):
                self._proc.stderr.close()

        self._proc.terminate()
        try:
            self._proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class SSHConnection(urllib3.connection.HTTPConnection):
    """Specialization of HTTPConnection to use a SSH forwarded socket."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: Union[float, urllib3.Timeout, None] = None,
        strict=False,
        **kwargs,  # pylint: disable=unused-argument
    ) -> None:
        """Initialize connection to SSHSocket for HTTP client.

        Args:
            host: Ignored.
            port: Ignored.
            timeout: Time to allow for operation.
            strict: Ignored.

        Keyword Args:
            uri: Full address of a Podman service including path to remote socket. Required.
            identity: path to file containing SSH key for authorization.
        """
        self.sock: Optional[socket.socket] = None
        self._stdio_bridge: Optional[_DialStdioBridge] = None

        connection_kwargs = kwargs.copy()
        connection_kwargs["port"] = port

        if timeout is not None:
            if isinstance(timeout, urllib3.Timeout):
                try:
                    connection_kwargs["timeout"] = float(timeout.total)
                except TypeError:
                    pass
            connection_kwargs["timeout"] = timeout

        self.uri = connection_kwargs.pop("uri")
        self.identity = connection_kwargs.pop("identity", None)

        super().__init__(host, **connection_kwargs)
        if logger.getEffectiveLevel() == logging.DEBUG:
            self.set_debuglevel(1)

    def connect(self) -> None:
        """Connect to Podman service via SSHSocket, falling back to dial-stdio."""
        try:
            sock = SSHSocket(self.uri, self.identity)
            sock.settimeout(self.timeout)
            sock.connect()
            self.sock = sock
            return
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("SSH stream-local forward failed (%s), trying dial-stdio", exc)
            with suppress(OSError):
                sock.close()

        bridge = _DialStdioBridge(self.uri, self.identity)
        self._stdio_bridge = bridge
        client = bridge.client_socket
        client.settimeout(self.timeout)
        self.sock = client

    def close(self) -> None:
        super().close()
        if self._stdio_bridge:
            self._stdio_bridge.close()
            self._stdio_bridge = None


class SSHConnectionPool(urllib3.HTTPConnectionPool):
    """Specialized HTTPConnectionPool for holding SSH connections."""

    ConnectionCls = SSHConnection  # pylint: disable=invalid-name


class SSHPoolManager(urllib3.PoolManager):
    """Specialized PoolManager for tracking SSH connections."""

    # pylint's special handling for namedtuple does not cover this usage
    # pylint: disable=invalid-name
    _PoolKey = collections.namedtuple(
        "_PoolKey", urllib3.poolmanager.PoolKey._fields + ("key_uri", "key_identity")
    )

    # Map supported schemes to Pool Classes
    _pool_classes_by_scheme = {
        "http": SSHConnectionPool,
        "http+ssh": SSHConnectionPool,
    }

    # Map supported schemes to Pool Key index generator
    _key_fn_by_scheme = {
        "http": functools.partial(_key_normalizer, _PoolKey),
        "http+ssh": functools.partial(_key_normalizer, _PoolKey),
    }

    def __init__(self, num_pools=10, headers=None, **kwargs):
        """Initialize SSHPoolManager.

        Args:
            num_pools: Number of SSH Connection pools to maintain.
            headers: Additional headers to add to operations.
        """
        super().__init__(num_pools, headers, **kwargs)
        self.pool_classes_by_scheme = SSHPoolManager._pool_classes_by_scheme
        self.key_fn_by_scheme = SSHPoolManager._key_fn_by_scheme


class SSHAdapter(HTTPAdapter):
    """Specialization of requests transport adapter for SSH forwarded UNIX domain sockets."""

    def __init__(
        self,
        uri: str,
        pool_connections: int = 9,
        pool_maxsize: int = 10,
        max_retries: int = DEFAULT_RETRIES,
        pool_block: int = DEFAULT_POOLBLOCK,
        **kwargs,
    ):  # pylint: disable=too-many-positional-arguments
        """Initialize SSHAdapter.

        Args:
            uri: Full address of a Podman service including path to remote socket.
                Format, ssh://<user>@<host>[:port]/run/podman/podman.sock?secure=True
            pool_connections: The number of connection pools to cache. Should be at least one less
                than pool_maxsize.
            pool_maxsize: The maximum number of connections to save in the pool.
                OpenSSH default is 10.
            max_retries: The maximum number of retries each connection should attempt.
            pool_block: Whether the connection pool should block for connections.

        Keyword Args:
            timeout (float):
            identity (str): Optional path to ssh identity key
        """
        self.poolmanager: Optional[SSHPoolManager] = None

        # Parsed for fail-fast side effects
        _ = urllib.parse.urlparse(uri)
        self._pool_kwargs = {"uri": uri}

        if "identity" in kwargs:
            path = pathlib.Path(kwargs.get("identity"))
            if not path.exists():
                raise FileNotFoundError(f"Identity file '{path}' does not exist.")
            self._pool_kwargs["identity"] = str(path)

        if "timeout" in kwargs:
            self._pool_kwargs["timeout"] = kwargs.get("timeout")

        super().__init__(pool_connections, pool_maxsize, max_retries, pool_block)

    def init_poolmanager(self, connections, maxsize, block=DEFAULT_POOLBLOCK, **kwargs):
        """Initialize SSHPoolManager to be used by SSHAdapter.

        Args:
            connections: The number of urllib3 connection pools to cache.
            maxsize: The maximum number of connections to save in the pool.
            block: Block when no free connections are available.
        """
        pool_kwargs = kwargs.copy()
        pool_kwargs.update(self._pool_kwargs)
        self.poolmanager = SSHPoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
        )
