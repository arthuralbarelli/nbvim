from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, TextArea

from .constants import CSS


class NbVim(App):
    CSS = CSS

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-row"):
            yield Static("[ ]", id="marker")
            yield TextArea.code_editor(language="python")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Grow the editor to fit all logical and wrapped lines."""
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)

def main() -> None:
    NbVim().run()


if __name__ == "__main__":
    main()

