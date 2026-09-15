import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nbformat.v4 import new_output

from nbvim.kernel import (
    ExecutionResult,
    KernelExecutionError,
    ProjectKernel,
    project_root_for,
    resolve_kernel_python,
)
from textual.widgets import Markdown, Static, TextArea

from nbvim.main import NbVim, format_output
from nbvim.model import CellModel, NotebookModel


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ProjectInterpreterTests(unittest.TestCase):
    def test_project_root_from_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            notebook = root / "notebooks" / "example.ipynb"
            notebook.parent.mkdir()
            notebook.touch()
            (root / "pyproject.toml").touch()
            _make_executable(root / ".venv" / "bin" / "python")

            self.assertEqual(project_root_for(notebook), root)

    def test_explicit_python_wins_over_virtual_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            _make_executable(root / "active" / "bin" / "python")
            explicit = _make_executable(root / "explicit" / "python")
            with patch.dict(os.environ, {"VIRTUAL_ENV": str(root / "active")}):
                os.environ.pop("CONDA_PREFIX", None)
                self.assertEqual(resolve_kernel_python(explicit), explicit)
                kernel = ProjectKernel(root / "notebook.ipynb", python=explicit)
                self.assertEqual(kernel.python, explicit)

    def test_virtual_env_interpreter_ignores_project_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            (root / "pyproject.toml").touch()
            _make_executable(root / ".venv" / "bin" / "python")
            active = _make_executable(root / "env" / "bin" / "python")
            with patch.dict(os.environ, {"VIRTUAL_ENV": str(root / "env")}):
                os.environ.pop("CONDA_PREFIX", None)
                self.assertEqual(resolve_kernel_python(), active)

    def test_conda_prefix_when_virtual_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            conda = _make_executable(root / "conda" / "bin" / "python")
            with patch.dict(os.environ, {"CONDA_PREFIX": str(root / "conda")}):
                os.environ.pop("VIRTUAL_ENV", None)
                self.assertEqual(resolve_kernel_python(), conda)

    def test_returns_none_without_active_environment(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("VIRTUAL_ENV", None)
            os.environ.pop("CONDA_PREFIX", None)
            self.assertIsNone(resolve_kernel_python())
            self.assertIsNone(ProjectKernel(Path("notebook.ipynb")).python)


class KernelExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_preserves_state_and_collects_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            kernel = ProjectKernel(
                Path(temporary_directory) / "example.ipynb",
                python=sys.executable,
            )
            try:
                first = await kernel.execute("value = 41\nprint('hello')\nvalue")
                second = await kernel.execute("value + 1")

                self.assertEqual(first.execution_count, 1)
                self.assertEqual(first.outputs[0]["output_type"], "stream")
                self.assertEqual(first.outputs[0]["text"], "hello\n")
                self.assertEqual(second.execution_count, 2)
                self.assertEqual(second.outputs[0]["data"]["text/plain"], "42")
            finally:
                await kernel.shutdown()

    async def test_error_output_is_not_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            kernel = ProjectKernel(
                Path(temporary_directory) / "example.ipynb",
                python=sys.executable,
            )
            try:
                result = await kernel.execute("raise ValueError('bad value')")
                self.assertEqual(result.outputs[0]["output_type"], "error")
                self.assertIn("ValueError", "\n".join(result.outputs[0]["traceback"]))
            finally:
                await kernel.shutdown()

    async def test_start_without_environment_raises(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("VIRTUAL_ENV", None)
            os.environ.pop("CONDA_PREFIX", None)
            kernel = ProjectKernel(Path("example.ipynb"))
            with self.assertRaises(KernelExecutionError) as raised:
                await kernel.start()
            self.assertIn("No active Python environment", str(raised.exception))


class OutputAndAppTests(unittest.IsolatedAsyncioTestCase):
    def test_output_formatting(self) -> None:
        self.assertEqual(
            format_output(new_output("stream", name="stdout", text="hello\n")),
            "hello\n",
        )
        self.assertEqual(
            format_output(
                new_output(
                    "error",
                    ename="ValueError",
                    evalue="bad",
                    traceback=["ValueError: bad"],
                )
            ),
            "ValueError: bad",
        )

    async def test_run_action_updates_focused_cell(self) -> None:
        class FakeKernel:
            async def execute(self, source: str) -> ExecutionResult:
                return ExecutionResult(
                    [new_output("stream", name="stdout", text=source)],
                    execution_count=1,
                )

            async def shutdown(self) -> None:
                pass

        app = NbVim(NotebookModel(cells=[CellModel(source="print('ok')")]))
        app.kernel = FakeKernel()
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()
            await app.run_action("run_cell")
            self.assertEqual(cell.model.execution_count, 1)
            self.assertEqual(cell.model.outputs[0]["text"], "print('ok')")


class CellTypeTests(unittest.IsolatedAsyncioTestCase):
    async def test_markdown_cells_render_and_convert_to_python(self) -> None:
        app = NbVim(
            NotebookModel(cells=[CellModel(cell_type="markdown", source="# Title")])
        )
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()

            markdown = cell.query_one(".cell-markdown", Markdown)
            editor = cell.query_one(TextArea)
            self.assertTrue(markdown.display)
            self.assertEqual(markdown.source, "# Title")
            self.assertFalse(editor.display)
            self.assertEqual(
                str(cell.query_one(".cell-footer", Static).render()), "markdown"
            )

            await pilot.press("m")
            await pilot.pause()

            self.assertEqual(cell.model.cell_type, "code")
            self.assertEqual(cell.model.source, "# Title")
            self.assertEqual(cell.language, "python")
            self.assertFalse(markdown.display)
            self.assertTrue(editor.display)
            self.assertEqual(editor.language, "python")

    async def test_python_cells_convert_to_rendered_markdown(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="## Heading")]))
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()

            await app.run_action("toggle_cell_type")
            await pilot.pause()

            markdown = cell.query_one(".cell-markdown", Markdown)
            self.assertEqual(cell.model.cell_type, "markdown")
            self.assertEqual(cell.language, "markdown")
            self.assertTrue(markdown.display)
            self.assertEqual(markdown.source, "## Heading")
            self.assertFalse(cell.query_one(TextArea).display)
            self.assertFalse(cell.query_one("OutputView").display)

    async def test_markdown_preview_returns_after_edit(self) -> None:
        app = NbVim(
            NotebookModel(cells=[CellModel(cell_type="markdown", source="# Title")])
        )
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()

            await app.run_action("edit_cell")
            await pilot.pause()
            self.assertTrue(cell.query_one(TextArea).display)
            self.assertFalse(cell.query_one(".cell-markdown", Markdown).display)

            await app.run_action("navigate_cell")
            await pilot.pause()
            self.assertFalse(cell.query_one(TextArea).display)
            markdown = cell.query_one(".cell-markdown", Markdown)
            self.assertTrue(markdown.display)
            self.assertEqual(markdown.source, "# Title")


if __name__ == "__main__":
    unittest.main()
