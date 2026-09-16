import base64
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from nbformat.v4 import new_output
from nbvim.kernel import (
    ExecutionResult,
    KernelExecutionError,
    ProjectKernel,
    _RETINA_STARTUP,
    _encode_binary_mime_data,
    _host_site_packages,
    _kernel_argv,
    _kernel_env,
    project_root_for,
    resolve_kernel_python,
)
from textual.widgets import Markdown, Static, TextArea

from nbvim.main import (
    NbVim,
    OutputImage,
    OutputView,
    _image_from_data,
    format_output,
    render_output,
)
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


class KernelArgvTests(unittest.TestCase):
    def test_uses_module_when_project_has_ipykernel(self) -> None:
        python = Path("/tmp/project/bin/python")
        self.assertEqual(
            _kernel_argv(python, inject_host_site=False),
            [
                str(python),
                "-m",
                "ipykernel_launcher",
                "--matplotlib=inline",
                "-f",
                "{connection_file}",
            ],
        )

    def test_injects_host_site_packages_when_missing(self) -> None:
        python = Path("/tmp/project/bin/python")
        argv = _kernel_argv(python, inject_host_site=True)
        self.assertEqual(argv[0], str(python))
        self.assertEqual(argv[1], "-c")
        for path in _host_site_packages():
            self.assertIn(path, argv[2])
        self.assertIn("ipykernel_launcher", argv[2])
        self.assertEqual(
            argv[-3:],
            ["--matplotlib=inline", "-f", "{connection_file}"],
        )

    def test_kernel_env_forces_inline_matplotlib_backend(self) -> None:
        with patch.dict(os.environ, {"MPLBACKEND": "macosx"}):
            env = _kernel_env()
        self.assertEqual(env["MPLBACKEND"], "module://matplotlib_inline.backend_inline")


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

    async def test_start_configures_retina_figures_without_history(self) -> None:
        executed: list[tuple[str, bool, bool]] = []

        class FakeClient:
            def execute(
                self,
                source: str,
                silent: bool = False,
                store_history: bool = True,
                allow_stdin: bool = False,
            ) -> str:
                executed.append((source, silent, store_history))
                return "startup-id"

            async def get_iopub_msg(self, timeout: float) -> dict[str, object]:
                return {
                    "parent_header": {"msg_id": "startup-id"},
                    "msg_type": "status",
                    "content": {"execution_state": "idle"},
                }

            def start_channels(self) -> None:
                pass

            async def wait_for_ready(self, timeout: float) -> None:
                pass

            def stop_channels(self) -> None:
                pass

        class FakeManager:
            kernel_spec = type("Spec", (), {"argv": []})()

            async def start_kernel(self, **kwargs: object) -> None:
                pass

            def client(self) -> FakeClient:
                return FakeClient()

            async def shutdown_kernel(self, now: bool = True) -> None:
                pass

        kernel = ProjectKernel(Path("example.ipynb"), python=sys.executable)
        with (
            patch("nbvim.kernel.AsyncKernelManager", return_value=FakeManager()),
            patch(
                "nbvim.kernel.subprocess.run",
                return_value=type("Result", (), {"returncode": 0})(),
            ),
        ):
            await kernel.start()

        self.assertEqual(
            executed,
            [(_RETINA_STARTUP, True, False)],
        )
        self.assertIn("retina", _RETINA_STARTUP)

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
    def _png_bytes(self) -> bytes:
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        image_bytes = io.BytesIO()
        image.save(image_bytes, format="PNG")
        return image_bytes.getvalue()

    def test_image_output_is_decoded_as_pil_image(self) -> None:
        output = new_output(
            "display_data",
            data={"image/png": base64.b64encode(self._png_bytes()).decode()},
        )

        rendered = render_output(output)

        self.assertIsInstance(rendered, Image.Image)
        self.assertEqual(rendered.size, (2, 2))

    def test_matplotlib_text_plain_does_not_hide_png(self) -> None:
        output = new_output(
            "display_data",
            data={
                "text/plain": "<Figure size 720x360 with 1 Axes>",
                "image/png": base64.b64encode(self._png_bytes()).decode(),
            },
        )

        self.assertIsInstance(render_output(output), Image.Image)

    def test_raw_png_bytes_are_rendered(self) -> None:
        png = self._png_bytes()
        output = {
            "output_type": "display_data",
            "data": {
                "text/plain": "<Figure size 720x360 with 1 Axes>",
                "image/png": png,
            },
        }

        self.assertIsInstance(_image_from_data(png), Image.Image)
        self.assertIsInstance(_image_from_data(memoryview(png)), Image.Image)
        self.assertIsInstance(
            _image_from_data("data:image/png;base64," + base64.b64encode(png).decode()),
            Image.Image,
        )
        self.assertIsInstance(render_output(output), Image.Image)

    def test_binary_mime_payloads_are_base64_encoded(self) -> None:
        png = self._png_bytes()
        encoded = _encode_binary_mime_data(
            {
                "text/plain": "<Figure size 720x360 with 1 Axes>",
                "image/png": png,
                "application/pdf": b"%PDF",
            }
        )

        self.assertEqual(encoded["text/plain"], "<Figure size 720x360 with 1 Axes>")
        self.assertEqual(encoded["image/png"], base64.b64encode(png).decode())
        self.assertEqual(encoded["application/pdf"], base64.b64encode(b"%PDF").decode())
        output = new_output("display_data", data=encoded)
        self.assertIsInstance(render_output(output), Image.Image)

    async def test_image_output_mounts_terminal_image_widget(self) -> None:
        output = new_output(
            "display_data",
            data={"image/png": base64.b64encode(self._png_bytes()).decode()},
        )
        app = NbVim(NotebookModel(cells=[CellModel(outputs=[output])]))
        async with app.run_test() as pilot:
            await pilot.pause()
            image = app.query_one(OutputImage)
            self.assertIsInstance(image, OutputImage)
            self.assertTrue(image.has_class("cell-output-image"))
            self.assertIsInstance(app.query_one(OutputView), OutputView)

    def test_image_render_is_cached_across_unchanged_repaints(self) -> None:
        # Regression test: textual_image's Image.render() unconditionally
        # discards and recreates its renderable on every call, which for
        # terminal graphics protocols (Kitty TGP, Sixel) means re-encoding and
        # re-transmitting the full image payload on every incidental repaint,
        # not just when the image or its size actually changes. That made
        # rendering slow, and made images that got repainted mid-transmission
        # (e.g. by scrolling past them) never finish rendering. nbvim's
        # OutputImage must reuse the same renderable while nothing changed.
        image = OutputImage(Image.new("RGB", (4, 4), (0, 0, 255)))

        first = image.render()
        second = image.render()
        third = image.render()

        self.assertIs(first, second)
        self.assertIs(second, third)

        # A genuine size change must still produce a fresh renderable.
        image.styles.width = 5
        resized = image.render()
        self.assertIsNot(resized, first)

        # Reassigning the image must also produce a fresh renderable.
        image.image = Image.new("RGB", (4, 4), (0, 255, 0))
        replaced = image.render()
        self.assertIsNot(replaced, resized)

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

    async def test_rerun_replaces_image_output_cleanly(self) -> None:
        # Regression test: re-running a cell used to call OutputView.remove_children()
        # and .mount() without awaiting either, so the old image widget's terminal
        # cleanup could race with the new image's mount. That left stale output
        # widgets around and made new images slow to appear or never render.
        first_png = self._png_bytes()
        second_image = Image.new("RGB", (3, 3), (0, 255, 0))
        second_bytes = io.BytesIO()
        second_image.save(second_bytes, format="PNG")

        class FakeKernel:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, source: str) -> ExecutionResult:
                self.calls += 1
                payload = first_png if self.calls == 1 else second_bytes.getvalue()
                output = new_output(
                    "display_data",
                    data={"image/png": base64.b64encode(payload).decode()},
                )
                return ExecutionResult([output], execution_count=self.calls)

            async def shutdown(self) -> None:
                pass

        app = NbVim(NotebookModel(cells=[CellModel(source="show(fig)")]))
        app.kernel = FakeKernel()
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()

            await app.run_action("run_cell_stay")
            await pilot.pause()
            images = list(app.query(OutputImage))
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].image.size, (2, 2))

            await app.run_action("run_cell_stay")
            await pilot.pause()
            images = list(app.query(OutputImage))
            # Exactly one image widget should remain: the old one removed,
            # the new one mounted, with nothing left over from the first run.
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].image.size, (3, 3))

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
