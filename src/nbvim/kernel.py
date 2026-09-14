"""Project-aware execution through a persistent Jupyter Python kernel."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from jupyter_client import AsyncKernelManager
from nbformat import NotebookNode
from nbformat.v4 import new_output


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


def resolve_project_python(
    notebook_path: str | Path,
    fallback: str | Path | None = None,
) -> Path:
    """Resolve the Python executable belonging to the notebook's project.

    UV and most Python project tools use ``.venv`` at the project root. If no
    project environment is present, the interpreter running nbvim is used.
    """
    root = project_root_for(notebook_path)
    virtualenv = root / ".venv"
    candidates = (
        ("Scripts", "python.exe"),
        ("bin", "python"),
        ("bin", "python3"),
    )
    for directory, executable in candidates:
        path = virtualenv / directory / executable
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return Path(fallback or sys.executable)


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
        self.python = (
            Path(python) if python else resolve_project_python(self.notebook_path)
        )
        self.startup_timeout = startup_timeout
        self.manager: AsyncKernelManager | None = None
        self.client = None
        self._execution_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the kernel if it is not already running."""
        if self.manager is not None and self.client is not None:
            return

        manager = AsyncKernelManager(kernel_name="python3")
        manager.kernel_spec.argv = [
            str(self.python),
            "-m",
            "ipykernel_launcher",
            "-f",
            "{connection_file}",
        ]
        try:
            await manager.start_kernel(cwd=str(self.project_root))
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

    async def execute(self, source: str) -> ExecutionResult:
        """Execute source and collect all notebook-compatible kernel outputs."""
        async with self._execution_lock:
            await self.start()
            assert self.client is not None

            try:
                message_id = self.client.execute(
                    source,
                    store_history=True,
                    allow_stdin=False,
                )
                outputs: list[NotebookNode] = []
                execution_count: int | None = None

                while True:
                    message = await self.client.get_iopub_msg(
                        timeout=self.startup_timeout
                    )
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
                            data=content.get("data", {}),
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
