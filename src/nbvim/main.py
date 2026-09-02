from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, TextArea


class NbVim(App):
    CSS = """
    #editor-row {
        height: auto;
    }

    #marker {
        width: 5;
        min-width: 5;
        padding: 0 1;
    }

    TextArea {
        width: 1fr;
        height: 4;
        min-height: 4;
        scrollbar-size: 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-row"):
            yield Static("[ ]", id="marker")
            yield TextArea.code_editor(language="python")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Grow the editor to fit all logical and wrapped lines."""
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)

if __name__ == "__main__":
    app = NbVim()
    app.run()

