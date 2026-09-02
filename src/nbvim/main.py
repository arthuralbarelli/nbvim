from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, TextArea


class NbVim(App):
    CSS = """
    #editor-row {
        height: 100%;
    }

    #marker {
        width: 5;
        min-width: 5;
        padding: 0 1;
    }

    TextArea {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-row"):
            yield Static("[ ]", id="marker")
            yield TextArea.code_editor(language="python")

if __name__ == "__main__":
    app = NbVim()
    app.run()

