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
        height: 3;
        min-height: 3;
        max-height: 15;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-row"):
            yield Static("[ ]", id="marker")
            yield TextArea.code_editor(language="python")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Grow the editor as lines are added, up to its maximum height."""
        line_count = max(3, event.text_area.text.count("\n") + 1)
        event.text_area.styles.height = line_count

if __name__ == "__main__":
    app = NbVim()
    app.run()

