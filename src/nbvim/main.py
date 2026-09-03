from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.binding import Binding
from textual.widgets import Static, TextArea

from .constants import CSS


class Cell(Horizontal):
    """A focusable container for a marker and a text area."""

    can_focus = True

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Grow the cell to fit all logical and wrapped lines."""
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)


class CellContainer(VerticalScroll):
    """Container responsible for adding and removing cells."""

    def compose(self) -> ComposeResult:
        yield self.create_cell()

    def create_cell(self) -> Cell:
        return Cell(
            Static("[ ]", classes="marker"),
            TextArea.code_editor(language="python"),
            classes="cell",
        )

    def get_focused_cell(self) -> Cell | None:
        node = self.app.focused
        while node is not None and node is not self:
            if isinstance(node, Cell):
                return node
            node = node.parent
        return None

    async def add_cell_after_focused(self) -> None:
        cell = self.create_cell()
        focused_cell = self.get_focused_cell()

        if focused_cell is None:
            await self.mount(cell)
        else:
            await self.mount(cell, after=focused_cell)

        cell.focus()

    async def add_cell_above_focused(self) -> None:
        cell = self.create_cell()
        focused_cell = self.get_focused_cell()

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

        cells[next_index].query_one(TextArea).focus()
        cell.remove()


class NbVim(App):
    CSS = CSS
    _delete_pending = False
    _delete_timer = None

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
        yield CellContainer(id="cells")

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
    NbVim().run()


if __name__ == "__main__":
    main()

