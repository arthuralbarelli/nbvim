from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Static, TextArea

from .constants import CSS


class NbVim(App):
    CSS = CSS

    def compose(self) -> ComposeResult:
        yield Button("Add cell", id="add-cell")
        with VerticalScroll(id="cells"):
            yield self.create_cell()

    def create_cell(self) -> Horizontal:
        return Horizontal(
            Static("[ ]", classes="marker"),
            TextArea.code_editor(language="python"),
            classes="cell",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-cell":
            self.query_one("#cells").mount(self.create_cell())

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Grow the cell to fit all logical and wrapped lines."""
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)

def main() -> None:
    NbVim().run()


if __name__ == "__main__":
    main()

