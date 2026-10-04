"""Reading preview: rendered markdown and code outputs, without cell source."""

import base64
import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image
from nbformat.v4 import new_output
from textual.widgets import Static, TextArea

from nbvim.kernel import ExecutionResult
from nbvim.main import NbVim, OutputImage, OutputView
from nbvim.model import CellModel, NotebookModel
from nbvim.vim_editor import VimMode, VimTextArea


def _cells(app: NbVim) -> list:
    return list(app.query("Cell"))


def _png_output() -> object:
    image = Image.new("RGB", (2, 2), (255, 0, 0))
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    return new_output(
        "display_data",
        data={"image/png": base64.b64encode(raw.getvalue()).decode()},
    )


def _status(app: NbVim) -> str:
    return str(app.query_one("#mode-status", Static).render()).strip()


class PreviewModeTests(unittest.IsolatedAsyncioTestCase):
    def _notebook(self) -> NotebookModel:
        table = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        return NotebookModel(
            cells=[
                CellModel(cell_type="markdown", source=f"# Title\n\n{table}"),
                CellModel(
                    source="print('secret-source')",
                    outputs=[
                        new_output("stream", name="stdout", text="visible-output\n")
                    ],
                    execution_count=3,
                ),
                CellModel(
                    source="secret_table = df",
                    outputs=[
                        new_output(
                            "execute_result",
                            data={"text/plain": "   A  B\n0  1  2\n"},
                            execution_count=1,
                        )
                    ],
                    execution_count=1,
                ),
                CellModel(source="show(fig)", outputs=[_png_output()]),
                CellModel(source="print('no-output-source')"),
            ]
        )

    async def test_preview_reads_the_note_and_edit_still_uses_the_editor(self) -> None:
        app = NbVim(self._notebook())
        async with app.run_test() as pilot:
            await pilot.pause()
            markdown, printed, table, image, empty = _cells(app)

            self.assertEqual(app.view_mode, "edit")
            self.assertEqual(_status(app), "edit")
            self.assertEqual(str(markdown.query_one(".marker", Static).render()), "")
            self.assertTrue(printed.query_one(TextArea).display)

            await pilot.press("p")
            await pilot.pause()

            self.assertEqual(app.view_mode, "preview")
            self.assertEqual(_status(app), "preview")
            # Preview is the reading view, not vim insert.
            self.assertFalse(printed._editing)
            self.assertNotIsInstance(app.focused, TextArea)
            self.assertIs(app.query_one("#cells").get_focused_cell(), markdown)

            self.assertTrue(markdown.query_one(".cell-markdown").display)
            self.assertEqual(
                markdown.query_one(".cell-markdown").source, markdown.model.source
            )
            self.assertFalse(markdown.query_one(TextArea).display)
            self.assertFalse(markdown.query_one(".cell-footer", Static).display)
            self.assertEqual(str(markdown.query_one(".marker", Static).render()), "")

            self.assertFalse(printed.query_one(TextArea).display)
            self.assertFalse(printed.query_one(".cell-footer", Static).display)
            self.assertFalse(printed.query_one(".marker", Static).display)
            self.assertTrue(printed.query_one(OutputView).display)
            printed_text = str(printed.query_one(".cell-output-text", Static).render())
            self.assertIn("visible-output", printed_text)
            self.assertNotIn("secret-source", printed_text)

            table_text = str(table.query_one(".cell-output-text", Static).render())
            self.assertTrue(table.query_one(OutputView).display)
            self.assertFalse(table.query_one(TextArea).display)
            self.assertIn("1  2", table_text)
            self.assertNotIn("secret_table", table_text)

            self.assertFalse(image.query_one(TextArea).display)
            self.assertTrue(image.query_one(OutputView).display)
            self.assertEqual(image.query_one(OutputImage).image.size, (2, 2))
            self.assertNotIn("show(fig)", str(image.query_one(OutputView).render()))

            self.assertFalse(empty.query_one(TextArea).display)
            self.assertFalse(empty.query_one(OutputView).display)
            quiet = empty.query_one(".cell-quiet", Static)
            self.assertTrue(quiet.display)
            self.assertNotIn("no-output-source", str(quiet.render()))
            self.assertEqual(
                empty.query_one(TextArea).text, "print('no-output-source')"
            )

            await pilot.press("j")
            await pilot.pause()
            self.assertIs(app.query_one("#cells").get_focused_cell(), printed)
            self.assertEqual(app.view_mode, "preview")
            self.assertFalse(printed.query_one(TextArea).display)

            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app.view_mode, "edit")
            self.assertEqual(_status(app), "edit")
            self.assertTrue(printed.query_one(TextArea).display)
            self.assertEqual(printed.query_one(TextArea).text, "print('secret-source')")
            self.assertFalse(empty.query_one(".cell-quiet", Static).display)
            self.assertTrue(empty.query_one(TextArea).display)
            self.assertEqual(str(markdown.query_one(".marker", Static).render()), "")
            self.assertEqual(str(printed.query_one(".marker", Static).render()), "[3]")
            self.assertTrue(printed.query_one(".marker", Static).display)

            await pilot.press(":")
            await pilot.pause()
            await pilot.press(*"preview", "enter")
            await pilot.pause()
            self.assertEqual(app.view_mode, "preview")
            self.assertEqual(_status(app), "preview")
            self.assertFalse(printed.query_one(TextArea).display)
            self.assertFalse(printed._editing)

            printed.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            editor = printed.query_one(VimTextArea)
            self.assertEqual(app.view_mode, "edit")
            self.assertEqual(_status(app), "edit")
            self.assertTrue(printed._editing)
            self.assertIs(app.focused, editor)
            self.assertEqual(editor.vim_mode, VimMode.INSERT)
            self.assertTrue(editor.display)
            self.assertIn(
                "-- INSERT --", str(printed.query_one(".cell-footer", Static).render())
            )

            await pilot.press("p")
            await pilot.pause()
            self.assertIn("p", editor.text)
            self.assertTrue(printed._editing)
            self.assertEqual(app.view_mode, "edit")
            self.assertEqual(_status(app), "edit")

    async def test_run_in_preview_shows_output_not_source(self) -> None:
        class FakeKernel:
            async def execute(self, source: str) -> ExecutionResult:
                return ExecutionResult(
                    [new_output("stream", name="stdout", text="ran-out\n")],
                    execution_count=4,
                )

            async def shutdown(self) -> None:
                return None

        app = NbVim(
            NotebookModel(
                cells=[
                    CellModel(source="print('hidden-src')"),
                    CellModel(source="print('next')"),
                ]
            )
        )
        app.kernel = FakeKernel()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()

            ran, nxt = _cells(app)
            self.assertEqual(app.view_mode, "preview")
            self.assertFalse(ran.query_one(TextArea).display)
            output = str(ran.query_one(".cell-output-text", Static).render())
            self.assertIn("ran-out", output)
            self.assertNotIn("hidden-src", output)
            self.assertFalse(nxt.query_one(TextArea).display)
            self.assertTrue(nxt.query_one(".cell-quiet", Static).display)
            self.assertNotIn(
                "print('next')", str(nxt.query_one(".cell-quiet").render())
            )


class PreviewReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "live.ipynb"
        self._mtime_offset = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _save(self, notebook: NotebookModel) -> None:
        notebook.save(self.path)
        self._mtime_offset += 1
        stamp = time.time() + self._mtime_offset
        os.utime(self.path, (stamp, stamp))

    async def test_reload_in_preview_keeps_source_hidden(self) -> None:
        notebook = NotebookModel(
            cells=[
                CellModel(id="alpha", source="print('old')"),
                CellModel(id="beta", cell_type="markdown", source="# Old"),
            ]
        )
        self._save(notebook)
        app = NbVim(NotebookModel.load(self.path), path=self.path)
        app.RELOAD_INTERVAL = 3600
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            updated = NotebookModel.load(self.path)
            code = next(cell for cell in updated.cells if cell.id == "alpha")
            note = next(cell for cell in updated.cells if cell.id == "beta")
            code.source = "print('from-disk')"
            code.execution_count = 8
            code.outputs = [new_output("stream", name="stdout", text="disk-out\n")]
            note.source = "# From disk"
            updated.cells.append(CellModel(id="gamma", source="print('empty')"))
            self._save(updated)
            await app._poll_notebook_file()
            await pilot.pause()

            self.assertEqual(app.view_mode, "preview")
            self.assertEqual(_status(app), "preview")
            cells = _cells(app)
            self.assertEqual(
                [cell.model.id for cell in cells], ["alpha", "beta", "gamma"]
            )
            code_cell, note_cell, empty_cell = cells
            self.assertFalse(code_cell.query_one(TextArea).display)
            self.assertEqual(code_cell.query_one(TextArea).text, "print('from-disk')")
            shown = str(code_cell.query_one(".cell-output-text", Static).render())
            self.assertIn("disk-out", shown)
            self.assertNotIn("from-disk", shown)
            self.assertTrue(note_cell.query_one(".cell-markdown").display)
            self.assertEqual(
                note_cell.query_one(".cell-markdown").source, "# From disk"
            )
            self.assertEqual(str(note_cell.query_one(".marker", Static).render()), "")
            self.assertFalse(empty_cell.query_one(TextArea).display)
            self.assertTrue(empty_cell.query_one(".cell-quiet", Static).display)
            self.assertNotIn(
                "print('empty')",
                str(empty_cell.query_one(".cell-quiet", Static).render()),
            )
