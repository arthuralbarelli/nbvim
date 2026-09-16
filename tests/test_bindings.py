import unittest

from nbformat.v4 import new_output

from nbvim.kernel import ExecutionResult, KernelExecutionError
from nbvim.main import CellContainer, NbVim
from nbvim.model import CellModel, NotebookModel
from nbvim.vim_editor import VimMode, VimTextArea
from textual.widgets import Static, TextArea


class FakeKernel:
    def __init__(
        self, result: ExecutionResult | None = None, error: str | None = None
    ) -> None:
        self.result = result or ExecutionResult(
            [new_output("stream", name="stdout", text="ok")],
            execution_count=1,
        )
        self.error = error
        self.calls: list[str] = []

    async def execute(self, source: str) -> ExecutionResult:
        self.calls.append(source)
        if self.error is not None:
            raise KernelExecutionError(self.error)
        return self.result

    async def shutdown(self) -> None:
        pass


def _cells(app: NbVim) -> list:
    return list(app.query("Cell"))


class CellModelCloneTests(unittest.TestCase):
    def test_clone_does_not_share_mutable_state(self) -> None:
        cell = CellModel(
            source="print(1)",
            metadata={"tag": "a"},
            outputs=[{"text": "1"}],
            execution_count=3,
        )
        clone = cell.clone()
        clone.source = "print(2)"
        clone.metadata["tag"] = "b"
        clone.outputs.append({"text": "2"})
        clone.execution_count = 4

        self.assertEqual(cell.source, "print(1)")
        self.assertEqual(cell.metadata, {"tag": "a"})
        self.assertEqual(cell.outputs, [{"text": "1"}])
        self.assertEqual(cell.execution_count, 3)


class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_then_paste_duplicates_cell_below(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="print(1)")]))
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()
            await pilot.press("c")
            await pilot.press("v")
            await pilot.pause()

            cells = _cells(app)
            self.assertEqual(len(cells), 2)
            self.assertEqual(cells[0].model.source, "print(1)")
            self.assertEqual(cells[1].model.source, "print(1)")
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), cells[1])

            cells[1].model.source = "print(2)"
            self.assertEqual(cells[0].model.source, "print(1)")
            self.assertEqual(app.query_one(CellContainer)._clipboard.source, "print(1)")

    async def test_paste_without_copy_keeps_single_cell(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="print(1)")]))
        async with app.run_test() as pilot:
            app.query_one("Cell").focus()
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            self.assertEqual(len(_cells(app)), 1)


class RunKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_stay_keeps_focus(self) -> None:
        app = NbVim(
            NotebookModel(
                cells=[
                    CellModel(source="print('ok')"),
                    CellModel(source="print('next')"),
                ]
            )
        )
        app.kernel = FakeKernel()
        async with app.run_test() as pilot:
            first, second = _cells(app)
            first.focus()
            await pilot.pause()
            await app.run_action("run_cell_stay")
            await pilot.pause()

            self.assertEqual(first.model.execution_count, 1)
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), first)
            self.assertEqual(len(_cells(app)), 2)
            self.assertIs(second, _cells(app)[1])

    async def test_run_advances_to_next_cell(self) -> None:
        app = NbVim(
            NotebookModel(
                cells=[
                    CellModel(source="print('ok')"),
                    CellModel(source="print('next')"),
                ]
            )
        )
        app.kernel = FakeKernel()
        async with app.run_test() as pilot:
            first, second = _cells(app)
            first.focus()
            await pilot.pause()
            await app.run_action("run_cell")
            await pilot.pause()

            self.assertEqual(first.model.execution_count, 1)
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), second)
            self.assertEqual(len(_cells(app)), 2)

    async def test_run_on_last_cell_inserts_a_new_cell(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="print('ok')")]))
        app.kernel = FakeKernel()
        async with app.run_test() as pilot:
            first = app.query_one("Cell")
            first.focus()
            await pilot.pause()
            await app.run_action("run_cell")
            await pilot.pause()

            cells = _cells(app)
            self.assertEqual(len(cells), 2)
            self.assertEqual(first.model.execution_count, 1)
            self.assertEqual(cells[1].model.source, "")
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), cells[1])

    async def test_failed_run_does_not_advance(self) -> None:
        app = NbVim(
            NotebookModel(
                cells=[CellModel(source="raise"), CellModel(source="print('next')")]
            )
        )
        app.kernel = FakeKernel(error="boom")
        async with app.run_test() as pilot:
            first, second = _cells(app)
            first.focus()
            await pilot.pause()
            await app.run_action("run_cell")
            await pilot.pause()

            self.assertIs(app.query_one(CellContainer).get_focused_cell(), first)
            self.assertEqual(len(_cells(app)), 2)
            self.assertIs(second, _cells(app)[1])

    async def test_markdown_run_advances_without_kernel(self) -> None:
        kernel = FakeKernel()
        app = NbVim(
            NotebookModel(
                cells=[
                    CellModel(cell_type="markdown", source="# Title"),
                    CellModel(source="print(1)"),
                ]
            )
        )
        app.kernel = kernel
        async with app.run_test() as pilot:
            first, second = _cells(app)
            first.focus()
            await pilot.pause()
            await app.run_action("run_cell")
            await pilot.pause()

            self.assertEqual(kernel.calls, [])
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), second)


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_cell_is_focused_on_mount(self) -> None:
        # NbVim.on_mount must focus the first cell explicitly: Textual's
        # default auto-focus otherwise lands on the CellContainer itself,
        # so get_focused_cell() returns None and the very first j/k/r press
        # on a freshly opened notebook is silently a no-op.
        app = NbVim(
            NotebookModel(
                cells=[CellModel(source="print(1)"), CellModel(source="print(2)")]
            )
        )
        kernel = FakeKernel()
        app.kernel = kernel
        async with app.run_test() as pilot:
            await pilot.pause()
            first, second = _cells(app)
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), first)

            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(kernel.calls, ["print(1)"])
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), second)

    async def test_move_down_skips_over_markdown_table_cells(self) -> None:
        # A markdown table renders via Textual's Markdown widget, whose table
        # cell widgets also carry the CSS class "cell". Notebook navigation
        # must not confuse those with our own Cell widgets.
        table_source = "| A | B |\n" "| --- | --- |\n" "| 1 | 2 |\n" "| 3 | 4 |\n"
        app = NbVim(
            NotebookModel(
                cells=[
                    CellModel(source="print('before')"),
                    CellModel(cell_type="markdown", source=table_source),
                    CellModel(source="print('after')"),
                ]
            )
        )
        async with app.run_test() as pilot:
            first, table_cell, last = _cells(app)
            first.focus()
            await pilot.pause()

            await pilot.press("j")
            await pilot.pause()
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), table_cell)

            await pilot.press("j")
            await pilot.pause()
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), last)

            await pilot.press("k")
            await pilot.pause()
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), table_cell)

            await pilot.press("k")
            await pilot.pause()
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), first)


class EditModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_insert_types_instead_of_moving_cells(self) -> None:
        app = NbVim(
            NotebookModel(cells=[CellModel(source="ab"), CellModel(source="next")])
        )
        async with app.run_test() as pilot:
            first, _second = _cells(app)
            first.focus()
            await pilot.pause()
            await app.run_action("edit_cell")
            await pilot.pause()

            editor = first.query_one(VimTextArea)
            self.assertEqual(editor.vim_mode, VimMode.INSERT)
            self.assertIn(
                "-- INSERT --", str(first.query_one(".cell-footer", Static).render())
            )

            await pilot.press("j")
            await pilot.pause()
            self.assertIn("j", editor.text)
            self.assertIs(app.query_one(CellContainer).get_focused_cell(), first)
            self.assertEqual(len(_cells(app)), 2)

    async def test_escape_enters_normal_then_navigation(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="hello")]))
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()
            await app.run_action("edit_cell")
            await pilot.pause()

            editor = cell.query_one(VimTextArea)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(editor.vim_mode, VimMode.NORMAL)
            self.assertTrue(cell._editing)
            self.assertIsInstance(app.focused, TextArea)

            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(cell._editing)
            self.assertIs(app.focused, cell)

    async def test_normal_line_and_char_operations(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="abc\ndef")]))
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()
            await app.run_action("edit_cell")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            editor = cell.query_one(VimTextArea)
            await pilot.press("d", "d")
            await pilot.pause()
            self.assertEqual(editor.text, "def")

            await pilot.press("y", "y")
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(editor.text, "def\ndef")

            editor.move_cursor((0, 0))
            await pilot.press("x")
            await pilot.pause()
            self.assertEqual(editor.text, "ef\ndef")

            await pilot.press("u")
            await pilot.pause()
            self.assertEqual(editor.text, "def\ndef")

    async def test_visual_delete_removes_selection(self) -> None:
        app = NbVim(NotebookModel(cells=[CellModel(source="abc")]))
        async with app.run_test() as pilot:
            cell = app.query_one("Cell")
            cell.focus()
            await pilot.pause()
            await app.run_action("edit_cell")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            editor = cell.query_one(VimTextArea)
            editor.move_cursor((0, 0))
            await pilot.press("v")
            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(editor.text, "bc")
            self.assertEqual(editor.vim_mode, VimMode.NORMAL)


if __name__ == "__main__":
    unittest.main()
