from textual.app import App, ComposeResult
from textual.widgets import TextArea

class NbVim(App):
    
    def compose(self) -> ComposeResult:
        yield TextArea.code_editor(language="python")

if __name__ == "__main__":
    app = NbVim()
    app.run()

