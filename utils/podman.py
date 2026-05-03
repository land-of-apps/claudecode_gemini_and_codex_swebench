"""Podman runtime helpers.

The SWE-bench harness uses the docker-py SDK, which talks to whatever socket
``DOCKER_HOST`` points to. We resolve the local Podman socket and inject it into
the subprocess environment so the harness runs against Podman.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional


class PodmanNotReady(RuntimeError):
    """Raised when Podman isn't installed or its API socket isn't reachable."""


def _resolve_socket() -> str:
    """Return a ``unix://`` URI for the local Podman API socket.

    Order of resolution:
    1. Honor an explicit ``DOCKER_HOST`` if the user already set one.
    2. On macOS, ask the running ``podman machine`` for its socket path.
    3. On Linux, use the rootless socket under ``$XDG_RUNTIME_DIR``,
       falling back to the rootful system socket.
    """
    explicit = os.environ.get("DOCKER_HOST")
    if explicit:
        return explicit

    if shutil.which("podman") is None:
        raise PodmanNotReady(
            "podman not found on PATH. Install it (macOS: `brew install podman`)."
        )

    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                [
                    "podman",
                    "machine",
                    "inspect",
                    "--format",
                    "{{.ConnectionInfo.PodmanSocket.Path}}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise PodmanNotReady(
                "Could not query the Podman machine. "
                "Run `podman machine init && podman machine start` first."
            ) from exc

        socket_path = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not socket_path:
            raise PodmanNotReady(
                "No Podman machine socket reported. Start it with `podman machine start`."
            )
        return f"unix://{socket_path}"

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    candidates = []
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "podman" / "podman.sock")
    candidates.append(Path("/run/podman/podman.sock"))

    for candidate in candidates:
        if candidate.exists():
            return f"unix://{candidate}"

    raise PodmanNotReady(
        "No Podman socket found. Start the user service with "
        "`systemctl --user start podman.socket` (or the system socket as root)."
    )


def podman_socket_uri() -> str:
    """Return the resolved Podman socket URI, raising :class:`PodmanNotReady` if missing."""
    return _resolve_socket()


def podman_env(base: Optional[dict] = None) -> dict:
    """Return an env dict with ``DOCKER_HOST`` pointing at Podman.

    Pass the result as ``env=`` to ``subprocess`` so the harness's docker-py
    client connects to Podman without relying on the caller's shell env.
    """
    env = dict(base if base is not None else os.environ)
    env["DOCKER_HOST"] = _resolve_socket()
    return env


def ensure_podman_ready() -> str:
    """Verify the Podman socket is reachable; return the URI on success."""
    uri = _resolve_socket()
    if uri.startswith("unix://"):
        path = Path(uri[len("unix://") :])
        if not path.exists():
            raise PodmanNotReady(
                f"Podman socket {path} does not exist. Is `podman machine` running?"
            )
    return uri
