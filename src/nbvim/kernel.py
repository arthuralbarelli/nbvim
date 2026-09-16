"""Project-aware execution through a persistent Jupyter Python kernel."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jupyter_client import AsyncKernelManager
from nbformat import NotebookNode
from nbformat.v4 import new_output

_INLINE_MPLBACKEND = "module://matplotlib_inline.backend_inline"
_BINARY_MIME_TYPES = {"application/pdf"}
_RETINA_STARTUP = "%config InlineBackend.figure_format = 'retina'"

_PYTHON_CANDIDATES = (
    ("Scripts", "python.exe"),
    ("bin", "python"),
    ("bin", "python3"),
)


class KernelExecutionError(RuntimeError):
    """Raised when the project Python kernel cannot be started or contacted."""


def project_root_for(notebook_path: str | Path) -> Path:
    """Find the nearest project directory for a notebook path."""
    path = Path(notebook_path).expanduser().resolve()
    start = path.parent if path.suffix == ".ipynb" else path

    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").exists() or (directory / ".venv").exists():
            return directory
    return start


def _python_in_environment(root: str | Path) -> Path | None:
    """Return the interpreter inside an environment prefix, if it exists."""
    for directory, executable in _PYTHON_CANDIDATES:
        path = Path(root) / directory / executable
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def resolve_kernel_python(python: str | Path | None = None) -> Path | None:
    """Resolve the interpreter that should run notebook cells.

    An explicit path wins. Otherwise the active virtualenv (``VIRTUAL_ENV``)
    or conda prefix (``CONDA_PREFIX``) is used. Returns ``None`` when no user
    environment is available so the editor can open without using nbvim's own
    interpreter.
    """
    if python is not None:
        return Path(python)

    for key in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(key)
        if not root:
            continue
        found = _python_in_environment(root)
        if found is not None:
            return found
    return None


def _host_site_packages() -> list[str]:
    """Site-packages of the interpreter running nbvim, used to bootstrap ipykernel."""
    import site

    paths: list[str] = []
    for path in site.getsitepackages():
        if path.startswith(sys.prefix) and path not in paths:
            paths.append(path)
    return paths


def _kernel_argv(python: Path, *, inject_host_site: bool) -> list[str]:
    """Build the ipykernel launch command for the project interpreter."""
    matplotlib_inline = ("--matplotlib=inline", "-f", "{connection_file}")
    if not inject_host_site:
        return [str(python), "-m", "ipykernel_launcher", *matplotlib_inline]
    script = (
        "import sys, runpy; "
        f"sys.path.extend({_host_site_packages()!r}); "
        "runpy.run_module('ipykernel_launcher', run_name='__main__')"
    )
    return [str(python), "-c", script, *matplotlib_inline]


def _kernel_env() -> dict[str, str]:
    """Environment for the kernel process, forcing matplotlib's inline backend."""
    env = os.environ.copy()
    env["MPLBACKEND"] = _INLINE_MPLBACKEND
    return env


def _is_binary_mime_type(mime_type: object) -> bool:
    if not isinstance(mime_type, str):
        return False
    return mime_type.startswith("image/") or mime_type in _BINARY_MIME_TYPES


def _encode_binary_mime_value(mime_type: object, value: object) -> object:
    """Base64-encode a binary mime payload so nbformat validation accepts it."""
    if not _is_binary_mime_type(mime_type):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes):
        return value
    return base64.b64encode(value).decode("ascii")


def _encode_binary_mime_data(data: object) -> dict[str, Any]:
    """Copy a mime bundle, encoding any raw image/pdf bytes as base64."""
    if not isinstance(data, dict):
        return {}
    return {
        mime_type: _encode_binary_mime_value(mime_type, value)
        for mime_type, value in data.items()
    }


@dataclass
class ExecutionResult:
    """Outputs and execution count returned by one kernel execution."""

    outputs: list[NotebookNode]
    execution_count: int | None


class ProjectKernel:
    """A lazily-started, reusable Python kernel for one project."""

    def __init__(
        self,
        notebook_path: str | Path,
        *,
        python: str | Path | None = None,
        startup_timeout: float = 15,
    ) -> None:
        self.notebook_path = Path(notebook_path)
        self.project_root = project_root_for(self.notebook_path)
        self.python = resolve_kernel_python(python)
        self.startup_timeout = startup_timeout
        self.manager: AsyncKernelManager | None = None
        self.client = None
        self._execution_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the kernel if it is not already running."""
        if self.manager is not None and self.client is not None:
            return
        if self.python is None:
            raise KernelExecutionError(
                "No active Python environment. Activate a virtualenv or conda "
                "env, or pass --python."
            )

        probe = subprocess.run(
            [str(self.python), "-c", "import ipykernel_launcher"],
            capture_output=True,
            text=True,
        )
        manager = AsyncKernelManager(kernel_name="python3")
        manager.kernel_spec.argv = _kernel_argv(
            self.python, inject_host_site=probe.returncode != 0
        )
        try:
            await manager.start_kernel(cwd=str(self.project_root), env=_kernel_env())
            client = manager.client()
            client.start_channels()
            await client.wait_for_ready(timeout=self.startup_timeout)
        except Exception as exc:
            await manager.shutdown_kernel(now=True)
            raise KernelExecutionError(
                f"Could not start the project Python kernel with {self.python}. "
                "Install ipykernel in that project environment and try again."
            ) from exc

        self.manager = manager
        self.client = client
        await self._run(_RETINA_STARTUP, store_history=False, silent=True)

    async def execute(self, source: str) -> ExecutionResult:
        """Execute source and collect all notebook-compatible kernel outputs."""
        async with self._execution_lock:
            await self.start()
            return await self._run(source, store_history=True, silent=False)

    async def _run(
        self,
        source: str,
        *,
        store_history: bool,
        silent: bool,
    ) -> ExecutionResult:
        """Send source to the kernel and collect iopub outputs until idle."""
        assert self.client is not None

        try:
            message_id = self.client.execute(
                source,
                silent=silent,
                store_history=store_history,
                allow_stdin=False,
            )
            outputs: list[NotebookNode] = []
            execution_count: int | None = None

            while True:
                message = await self.client.get_iopub_msg(timeout=self.startup_timeout)
                parent_id = message.get("parent_header", {}).get("msg_id")
                if parent_id != message_id:
                    continue

                message_type = message["msg_type"]
                content = message["content"]
                if (
                    message_type == "status"
                    and content.get("execution_state") == "idle"
                ):
                    break
                if message_type == "execute_input":
                    execution_count = content.get("execution_count")
                elif message_type == "stream":
                    outputs.append(
                        new_output(
                            "stream",
                            name=content.get("name", "stdout"),
                            text=content.get("text", ""),
                        )
                    )
                elif message_type in {"display_data", "execute_result"}:
                    output = new_output(
                        message_type,
                        data=_encode_binary_mime_data(content.get("data", {})),
                        metadata=content.get("metadata", {}),
                    )
                    if message_type == "execute_result":
                        output["execution_count"] = content.get(
                            "execution_count", execution_count
                        )
                    outputs.append(output)
                elif message_type == "error":
                    outputs.append(
                        new_output(
                            "error",
                            ename=content.get("ename", ""),
                            evalue=content.get("evalue", ""),
                            traceback=content.get("traceback", []),
                        )
                    )
        except Exception as exc:
            await self.shutdown()
            raise KernelExecutionError(
                "The project Python kernel stopped responding."
            ) from exc

        return ExecutionResult(outputs, execution_count)

    async def shutdown(self) -> None:
        """Stop the kernel and release its channels."""
        if self.client is not None:
            self.client.stop_channels()
        if self.manager is not None:
            await self.manager.shutdown_kernel(now=True)
        self.client = None
        self.manager = None
