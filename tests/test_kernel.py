import stat
import tempfile
import unittest
from pathlib import Path

from nbformat.v4 import new_output

from nbvim.kernel import (
    ExecutionResult,
    ProjectKernel,
    project_root_for,
    resolve_project_python,
)
from nbvim.main import NbVim, format_output
from nbvim.model import CellModel, NotebookModel


class ProjectInterpreterTests(unittest.TestCase):
    def test_project_root_and_virtualenv_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            notebook = root / "notebooks" / "example.ipynb"
            notebook.parent.mkdir()
            notebook.touch()
            (root / "pyproject.toml").touch()
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            python.chmod(python.stat().st_mode | stat.S_IXUSR)

            self.assertEqual(project_root_for(notebook), root.resolve())
            self.assertEqual(resolve_project_python(notebook), python)

    def test_falls_back_to_supplied_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            notebook = Path(temporary_directory) / "example.ipynb"
            fallback = Path(temporary_directory) / "python"
            self.assertEqual(resolve_project_python(notebook, fallback), fallback)


class KernelExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_preserves_state_and_collects_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            kernel = ProjectKernel(Path(temporary_directory) / "example.ipynb")
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
            kernel = ProjectKernel(Path(temporary_directory) / "example.ipynb")
            try:
                result = await kernel.execute("raise ValueError('bad value')")
                self.assertEqual(result.outputs[0]["output_type"], "error")
                self.assertIn("ValueError", "\n".join(result.outputs[0]["traceback"]))
            finally:
                await kernel.shutdown()


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


if __name__ == "__main__":
    unittest.main()
