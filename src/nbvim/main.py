from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.binding import Binding
from textual.widgets import Static, TextArea

from .constants import CSS


class Cell(Horizontal):
    """A focusable container for a marker and a text area."""

    can_focus = True


class NbVim(App):
    CSS = CSS
    BINDINGS = [
        Binding("b", "add_cell", "Add cell", priority=True),
        Binding("d", "delete_cell", "Delete cell", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="cells"):
            yield self.create_cell()

    def create_cell(self) -> Cell:
        return Cell(
            Static("[ ]", classes="marker"),
            TextArea.code_editor(language="python"),
            classes="cell",
        )

    def get_focused_cell(self) -> Cell | None:
        node = self.focused
        while node is not None:
            if isinstance(node, Cell):
                return node
            node = node.parent
        return None

    async def action_add_cell(self) -> None:
        cell = self.create_cell()
        focused_cell = self.get_focused_cell()
        cells = self.query_one("#cells")

        if focused_cell is None:
            await cells.mount(cell)
        else:
            await cells.mount(cell, after=focused_cell)

        cell.query_one(TextArea).focus()

    def action_delete_cell(self) -> None:
        """Delete the cell containing the focused widget."""
        cell = self.get_focused_cell()
        cells = list(self.query(".cell"))
        if cell is None or cell not in cells or len(cells) == 1:
            return

        cell_index = cells.index(cell)
        next_cell = cells[min(cell_index + 1, len(cells) - 1)]
        next_cell.query_one(TextArea).focus()
        cell.remove()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Grow the cell to fit all logical and wrapped lines."""
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)

def main() -> None:
    NbVim().run()


if __name__ == "__main__":
    main()

