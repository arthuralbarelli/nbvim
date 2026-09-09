import argparse
from pathlib import Path

from textual.app import App, ComposeResult
from textual.events import Key
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.binding import Binding
from textual.widgets import Input, Static, TextArea

from .constants import CSS
from .model import CellModel, NotebookModel


class Cell(Horizontal):
    """A focusable cell with a marker, editor, and language footer."""

    can_focus = True

    def __init__(
        self,
        model: CellModel | None = None,
        language: str = "python",
        **kwargs,
    ) -> None:
        kwargs.setdefault("classes", "cell")
        super().__init__(**kwargs)
        self.model = model or CellModel()
        self.language = language

    def compose(self) -> ComposeResult:
        yield Static("[ ]", classes="marker")
        with Vertical(classes="cell-editor"):
            yield TextArea.code_editor(
                text=self.model.source,
                language=self.language,
            )
            yield Static(self.language, classes="cell-footer")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Keep the notebook model and cell height in sync with the editor."""
        self.model.source = event.text_area.text
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)


class CellContainer(VerticalScroll):
    """Container responsible for displaying and editing notebook cells."""

    def __init__(self, notebook: NotebookModel | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.notebook = notebook or NotebookModel.new()

    def compose(self) -> ComposeResult:
        if not self.notebook.cells:
            self.notebook.add_cell()
        for cell in self.notebook.cells:
            yield self.create_cell(cell)

    def create_cell(self, model: CellModel | None = None) -> Cell:
        return Cell(model=model)

    def get_focused_cell(self) -> Cell | None:
        node = self.app.focused
        while node is not None and node is not self:
            if isinstance(node, Cell):
                return node
            node = node.parent
        return None

    async def add_cell_after_focused(self) -> None:
        focused_cell = self.get_focused_cell()
        model = CellModel()
        cell_index = (
            self.notebook.cells.index(focused_cell.model) + 1
            if focused_cell is not None
            else len(self.notebook.cells)
        )
        self.notebook.add_cell(model, cell_index)
        cell = self.create_cell(model)

        if focused_cell is None:
            await self.mount(cell)
        else:
            await self.mount(cell, after=focused_cell)

        cell.focus()

    async def add_cell_above_focused(self) -> None:
        focused_cell = self.get_focused_cell()
        model = CellModel()
        cell_index = (
            self.notebook.cells.index(focused_cell.model)
            if focused_cell is not None
            else 0
        )
        self.notebook.add_cell(model, cell_index)
        cell = self.create_cell(model)

        if focused_cell is None:
            await self.mount(cell)
        else:
            await self.mount(cell, before=focused_cell)

        cell.focus()

    def edit_focused_cell(self) -> None:
        cell = self.get_focused_cell()
        if cell is not None:
            cell.query_one(TextArea).focus()

    def navigate_focused_cell(self) -> None:
        cell = self.get_focused_cell()
        if cell is not None:
            cell.focus()

    def focus_relative_cell(self, offset: int) -> None:
        cells = list(self.query(".cell"))
        if not cells:
            return

        focused_cell = self.get_focused_cell()
        if focused_cell is None:
            target_index = 0 if offset > 0 else len(cells) - 1
        else:
            target_index = cells.index(focused_cell) + offset
            target_index = max(0, min(target_index, len(cells) - 1))

        cells[target_index].focus()

    def delete_focused_cell(self) -> None:
        cell = self.get_focused_cell()
        cells = list(self.query(".cell"))
        if cell is None or cell not in cells or len(cells) == 1:
            return

        cell_index = cells.index(cell)
        next_index = min(cell_index + 1, len(cells) - 1)
        if next_index == cell_index:
            next_index -= 1

        cells[next_index].focus()
        self.notebook.remove_cell(self.notebook.cells.index(cell.model))
        cell.remove()


class NbVim(App):
    CSS = CSS
    _delete_pending = False
    _delete_timer = None

    def __init__(
        self,
        notebook: NotebookModel | None = None,
        path: Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.notebook = notebook or NotebookModel.new()
        self.path = path
        self.save_on_exit = True

    BINDINGS = [
        Binding("b", "add_cell", "Add cell", priority=True),
        Binding("d", "delete_cell", "Delete cell", priority=True),
        Binding("j", "move_down", "Move to next cell", priority=True),
        Binding("k", "move_up", "Move to previous cell", priority=True),
        Binding("a", "add_cell_above", "Add cell above", priority=True),
        Binding("enter", "edit_cell", "Edit cell"),
        Binding("escape", "navigate_cell", "Navigate cells", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield CellContainer(self.notebook, id="cells")
        yield Input(id="command-bar")

    def on_key(self, event: Key) -> None:
        """Open the command bar when ``:`` is pressed in navigation mode."""
        if event.character == ":" and not isinstance(self.focused, TextArea):
            command_bar = self.query_one("#command-bar", Input)
            command_bar.value = ":"
            command_bar.styles.display = "block"
            command_bar.focus()
            # Keep the command prefix intact; focus may otherwise select it.
            command_bar.cursor_position = 1
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value
        command_bar = event.input
        command_bar.value = ""
        command_bar.styles.display = "none"

        if command.startswith(":"):
            command = command[1:]

        if command == "w":
            self.save_notebook()
        elif command == "q":
            self.save_on_exit = False
            self.exit()
        elif command == "wq":
            self.save_notebook()
            self.exit()
        elif command == "q!":
            self.save_on_exit = False
            self.exit()
        else:
            self.notify(f"Not an nbvim command: :{command}", severity="error")
            command_bar.styles.display = "block"
            command_bar.focus()
            return

        self.query_one(CellContainer).navigate_focused_cell()

    def save_notebook(self) -> None:
        if self.path is None:
            self.notify("No notebook path specified", severity="error")
            return
        self.notebook.save(self.path)

    async def action_add_cell(self) -> None:
        await self.query_one(CellContainer).add_cell_after_focused()

    def action_move_down(self) -> None:
        self.query_one(CellContainer).focus_relative_cell(1)

    def action_move_up(self) -> None:
        self.query_one(CellContainer).focus_relative_cell(-1)

    async def action_add_cell_above(self) -> None:
        await self.query_one(CellContainer).add_cell_above_focused()

    def action_edit_cell(self) -> None:
        self.query_one(CellContainer).edit_focused_cell()

    def action_navigate_cell(self) -> None:
        self.query_one(CellContainer).navigate_focused_cell()

    def action_delete_cell(self) -> None:
        """Delete the focused cell after two consecutive presses of ``d``."""
        if self._delete_pending:
            self._delete_pending = False
            if self._delete_timer is not None:
                self._delete_timer.stop()
                self._delete_timer = None
            self.query_one(CellContainer).delete_focused_cell()
        else:
            self._delete_pending = True
            self._delete_timer = self.set_timer(0.5, self._reset_delete)

    def _reset_delete(self) -> None:
        self._delete_pending = False
        self._delete_timer = None

def main() -> None:
    parser = argparse.ArgumentParser(prog="nbvim")
    parser.add_argument(
        "notebook",
        nargs="?",
        type=Path,
        help="notebook to open or create",
    )
    args = parser.parse_args()

    if args.notebook is None:
        NbVim().run()
        return

    notebook = (
        NotebookModel.load(args.notebook)
        if args.notebook.exists()
        else NotebookModel.new()
    )
    # Create the file before starting the UI, so the requested path exists even
    # if the user exits without making any changes.
    notebook.save(args.notebook)
    try:
        app = NbVim(notebook, path=args.notebook)
        app.run()
    finally:
        if app.save_on_exit:
            notebook.save(args.notebook)


if __name__ == "__main__":
    main()

