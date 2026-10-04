import os
import tempfile
import time
import unittest
from pathlib import Path

from nbformat.v4 import new_output
from textual.widgets import Static

from nbvim.main import NbVim
from nbvim.model import CellModel, NotebookModel
from nbvim.vim_editor import VimTextArea


def _cells(app: NbVim) -> list:
    return list(app.query("Cell"))


class NotebookReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "live.ipynb"
        self._mtime_offset = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bump_mtime(self) -> None:
        """Move mtime forward so a same-size rewrite cannot hide behind the clock."""
        self._mtime_offset += 1
        stamp = time.time() + self._mtime_offset
        os.utime(self.path, (stamp, stamp))

    def _save(self, notebook: NotebookModel) -> None:
        notebook.save(self.path)
        self._bump_mtime()

    def _app(self, notebook: NotebookModel) -> NbVim:
        self._save(notebook)
        app = NbVim(NotebookModel.load(self.path), path=self.path)
        app.RELOAD_INTERVAL = 3600
        return app

    async def test_external_reload_updates_ui_and_self_save_does_not(self) -> None:
        app = self._app(
            NotebookModel(
                cells=[
                    CellModel(id="alpha", source="print('old')"),
                    CellModel(id="beta", source="print('next')"),
                ]
            )
        )
        applies = 0
        saves = 0

        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(
                any(timer.name == "notebook-file-watch" for timer in app._timers)
            )
            original_apply = app._apply_disk_notebook
            original_save = app.save_notebook

            async def count_apply(loaded: NotebookModel, preserve=None) -> None:
                nonlocal applies
                applies += 1
                await original_apply(loaded, preserve=preserve)

            def count_save() -> None:
                nonlocal saves
                saves += 1
                original_save()

            app._apply_disk_notebook = count_apply
            app.save_notebook = count_save

            alpha, beta = _cells(app)
            beta.focus()
            await pilot.pause()
            alpha.query_one(VimTextArea).text = "print('saved')"
            await pilot.pause()
            app.save_notebook()
            # A timestamp-only change of the bytes nbvim just wrote must not
            # reload. That is the loop: save, notice mtime, reload, save again.
            self._bump_mtime()
            await app._poll_notebook_file()
            await pilot.pause()

            self.assertEqual(saves, 1)
            self.assertEqual(applies, 0)
            self.assertIs(_cells(app)[0], alpha)
            self.assertEqual(alpha.query_one(VimTextArea).text, "print('saved')")

            updated = NotebookModel.load(self.path)
            beta_cell = next(cell for cell in updated.cells if cell.id == "beta")
            alpha_cell = next(cell for cell in updated.cells if cell.id == "alpha")
            beta_cell.source = "print('from-agent')"
            beta_cell.execution_count = 4
            beta_cell.outputs = [
                new_output("stream", name="stdout", text="ran\n"),
            ]
            alpha_cell.source = "print('was-first')"
            updated.cells = [
                beta_cell,
                alpha_cell,
                CellModel(id="gamma", cell_type="markdown", source="# Note"),
            ]
            self._save(updated)
            await app._poll_notebook_file()
            await pilot.pause()

            self.assertEqual(applies, 1)
            self.assertEqual(saves, 1)
            cells = _cells(app)
            self.assertEqual(
                [cell.model.id for cell in cells], ["beta", "alpha", "gamma"]
            )
            self.assertIs(app.query_one("#cells").get_focused_cell(), beta)
            self.assertEqual(beta.model.source, "print('from-agent')")
            self.assertEqual(beta.model.execution_count, 4)
            self.assertEqual(beta.model.outputs[0]["text"], "ran\n")
            self.assertEqual(
                str(beta.query_one(".marker", Static).render()),
                "[4]",
            )
            self.assertIn(
                "ran",
                str(beta.query_one(".cell-output-text", Static).render()),
            )
            self.assertEqual(cells[1].query_one(VimTextArea).text, "print('was-first')")
            self.assertEqual(str(cells[2].query_one(".marker", Static).render()), "")
            self.assertEqual(cells[2].query_one(".cell-markdown").source, "# Note")

            await app._poll_notebook_file()
            await pilot.pause()
            self.assertEqual(applies, 1)
            self.assertEqual(saves, 1)

    async def test_external_reload_keeps_the_cell_being_edited(self) -> None:
        app = self._app(
            NotebookModel(
                cells=[
                    CellModel(id="alpha", source="print(1)"),
                    CellModel(id="beta", source="print(2)"),
                ]
            )
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            alpha = _cells(app)[0]
            alpha.focus()
            await pilot.pause()
            await app.run_action("edit_cell")
            await pilot.pause()
            editor = alpha.query_one(VimTextArea)
            await pilot.press("x")
            await pilot.pause()
            self.assertEqual(editor.text, "xprint(1)")
            cursor = editor.cursor_location

            updated = NotebookModel.load(self.path)
            updated.cells[0].source = "FROM_DISK"
            updated.cells[0].execution_count = 9
            updated.cells[0].outputs = [
                new_output("stream", name="stdout", text="out\n"),
            ]
            updated.cells[1].source = "disk-beta"
            updated.cells.append(
                CellModel(id="gamma", cell_type="markdown", source="# Note")
            )
            self._save(updated)
            await app._poll_notebook_file()
            await pilot.pause()

            self.assertIs(app.focused, editor)
            self.assertEqual(editor.text, "xprint(1)")
            self.assertEqual(editor.cursor_location, cursor)
            self.assertEqual(alpha.model.execution_count, 9)
            self.assertIn(
                "out",
                str(alpha.query_one(".cell-output-text", Static).render()),
            )
            cells = _cells(app)
            self.assertEqual(
                [cell.model.id for cell in cells], ["alpha", "beta", "gamma"]
            )
            self.assertEqual(cells[1].query_one(VimTextArea).text, "disk-beta")
            self.assertEqual(str(cells[2].query_one(".marker", Static).render()), "")
            self.assertEqual(cells[2].query_one(".cell-markdown").source, "# Note")

            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(editor.text, "xyprint(1)")

            self._save(NotebookModel(cells=[CellModel(id="beta", source="only-beta")]))
            await app._poll_notebook_file()
            await pilot.pause()
            self.assertEqual(editor.text, "xyprint(1)")
            self.assertIs(app.focused, editor)
            self.assertEqual(
                [cell.model.source for cell in _cells(app)],
                ["xyprint(1)", "only-beta"],
            )

    async def test_type_change_waits_until_navigation_and_keeps_the_edit(self) -> None:
        app = self._app(
            NotebookModel(
                cells=[
                    CellModel(id="alpha", source="print(1)"),
                    CellModel(id="beta", source="print(2)"),
                ]
            )
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            alpha = _cells(app)[0]
            alpha.focus()
            await pilot.pause()
            await app.run_action("edit_cell")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()

            updated = NotebookModel.load(self.path)
            updated.cells[0].cell_type = "markdown"
            updated.cells[0].source = "# Disk"
            updated.cells[0].outputs = []
            updated.cells[0].execution_count = None
            updated.cells[1].source = "later"
            self._save(updated)
            await app._poll_notebook_file()
            await pilot.pause()

            editor = alpha.query_one(VimTextArea)
            self.assertEqual(editor.text, "xprint(1)")
            self.assertTrue(editor.display)
            self.assertEqual(alpha.model.cell_type, "code")
            self.assertEqual(_cells(app)[1].query_one(VimTextArea).text, "later")

            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            alpha = _cells(app)[0]
            self.assertEqual(alpha.model.cell_type, "markdown")
            self.assertEqual(alpha.model.source, "xprint(1)")
            self.assertEqual(alpha.query_one(".cell-markdown").source, "xprint(1)")
            self.assertFalse(alpha.query_one(VimTextArea).display)
            self.assertEqual(str(alpha.query_one(".marker", Static).render()), "")
            self.assertEqual(_cells(app)[1].model.source, "later")

    async def test_reload_waits_until_the_running_cell_finishes(self) -> None:
        app = self._app(NotebookModel(cells=[CellModel(id="alpha", source="print(1)")]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cell_running = True
            self._save(NotebookModel(cells=[CellModel(id="alpha", source="print(2)")]))
            await app._poll_notebook_file()
            await pilot.pause()

            self.assertTrue(app._reload_pending)
            self.assertEqual(_cells(app)[0].model.source, "print(1)")

            app._cell_running = False
            await app._poll_notebook_file()
            await pilot.pause()
            self.assertEqual(_cells(app)[0].model.source, "print(2)")
