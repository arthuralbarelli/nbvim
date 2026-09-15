import argparse
import base64
import binascii
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from rich.console import ConsoleOptions, Group, RenderResult
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.events import Key
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.binding import Binding
from textual.widgets import Input, Markdown, Static, TextArea

from .kernel import KernelExecutionError, ProjectKernel
from .model import CellModel, NotebookModel


MAX_TERMINAL_IMAGE_WIDTH = 60


def _text_value(value: object) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value)


class TerminalImage:
    """Render an image as colored Unicode half-blocks."""

    def __init__(self, image: Image.Image) -> None:
        self.image = image.convert("RGB")

    def __rich_console__(
        self, console: object, options: ConsoleOptions
    ) -> RenderResult:
        """Yield terminal rows, adapting the image to the available width."""
        if self.image.width == 0 or self.image.height == 0:
            return

        width = max(1, min(self.image.width, options.max_width, MAX_TERMINAL_IMAGE_WIDTH))
        height = max(1, round(self.image.height * width / self.image.width))
        image = self.image.resize((width, height), Image.Resampling.LANCZOS)
        pixels = image.load()
        style_cache: dict[tuple[tuple[int, int, int], tuple[int, int, int]], Style] = {}

        for y in range(0, height, 2):
            row = Text()
            for x in range(width):
                top = pixels[x, y]
                bottom = pixels[x, y + 1] if y + 1 < height else (0, 0, 0)
                colors = (top, bottom)
                style = style_cache.get(colors)
                if style is None:
                    style = Style(
                        color=f"rgb({top[0]},{top[1]},{top[2]})",
                        bgcolor=f"rgb({bottom[0]},{bottom[1]},{bottom[2]})",
                    )
                    style_cache[colors] = style
                row.append("▀", style=style)
            yield row


def _image_from_data(value: object) -> Image.Image | None:
    """Decode a Jupyter base64 image payload."""
    try:
        encoded = _text_value(value).encode("ascii")
        image = Image.open(BytesIO(base64.b64decode(encoded)))
        image.load()
    except (ValueError, UnicodeError, binascii.Error, UnidentifiedImageError, OSError):
        return None

    if image.mode == "RGBA":
        background = Image.new("RGBA", image.size, (0, 0, 0, 255))
        image = Image.alpha_composite(background, image)
    return image.convert("RGB")


def format_output(output: object) -> str:
    """Convert a notebook output into readable terminal text."""
    output_type = output.get("output_type") if isinstance(output, dict) else None
    if output_type == "stream":
        return _text_value(output.get("text", ""))
    if output_type == "error":
        return "\n".join(output.get("traceback", []))
    if output_type in {"display_data", "execute_result"}:
        data = output.get("data", {})
        if "text/plain" in data:
            return _text_value(data["text/plain"])
        for media_type in (
            "text/html",
            "image/svg+xml",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        ):
            if media_type in data:
                return f"[{media_type} output]"
        return "[display data]"
    return ""


def render_output(output: object) -> str | TerminalImage:
    """Convert a notebook output into a Rich-compatible terminal renderable."""
    if isinstance(output, dict) and output.get("output_type") in {
        "display_data",
        "execute_result",
    }:
        data = output.get("data", {})
        if isinstance(data, dict):
            for media_type in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                if media_type in data:
                    image = _image_from_data(data[media_type])
                    if image is not None:
                        return TerminalImage(image)
    return format_output(output)


class OutputView(Static):
    """Render the outputs currently stored on a cell."""

    def __init__(self, outputs: list[object] | None = None, **kwargs) -> None:
        self.outputs = outputs or []
        super().__init__(self.render_outputs(), markup=False, **kwargs)

    def render_outputs(self) -> Group:
        rendered = []
        for output in self.outputs:
            item = render_output(output)
            if item != "":
                rendered.append(Text(item) if isinstance(item, str) else item)
        return Group(*rendered)

    def update_outputs(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.update(self.render_outputs())


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
        self.language = "markdown" if self.model.cell_type == "markdown" else language
        self._editing = False

    def compose(self) -> ComposeResult:
        is_markdown = self.model.cell_type == "markdown"
        markdown = Markdown(self.model.source, classes="cell-markdown")
        markdown.display = is_markdown
        editor = TextArea.code_editor(
            text=self.model.source,
            language=self.language,
        )
        editor.display = not is_markdown
        output = OutputView(self.model.outputs, classes="cell-output")
        output.display = not is_markdown
        yield Static("[ ]", classes="marker")
        with Vertical(classes="cell-editor"):
            yield markdown
            yield editor
            yield output
            yield Static(self.language, classes="cell-footer")

    def on_mount(self) -> None:
        if self.model.cell_type == "code" and self.model.execution_count is not None:
            self.query_one(".marker", Static).update(f"[{self.model.execution_count}]")
        self._sync_editor_height()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Keep the notebook model and cell height in sync with the editor."""
        self.model.source = event.text_area.text
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)

    def enter_edit(self) -> None:
        self._editing = True
        self.apply_presentation()
        self.query_one(TextArea).focus()

    def exit_edit(self) -> None:
        self._editing = False
        self.apply_presentation()

    def toggle_type(self) -> None:
        """Switch the cell between markdown and Python code."""
        if self.model.cell_type == "markdown":
            self.model.cell_type = "code"
            self.language = "python"
        else:
            self.model.cell_type = "markdown"
            self.language = "markdown"
            self.model.outputs = []
            self.model.execution_count = None
            self.query_one(OutputView).update_outputs([])
            self.query_one(".marker", Static).update("[ ]")
        self._editing = False
        self.apply_presentation()

    def apply_presentation(self) -> None:
        """Show a markdown preview, or the code editor, based on cell type."""
        is_markdown = self.model.cell_type == "markdown"
        show_editor = not is_markdown or self._editing
        markdown = self.query_one(".cell-markdown", Markdown)
        editor = self.query_one(TextArea)
        markdown.display = is_markdown and not self._editing
        if markdown.display:
            markdown.update(self.model.source)
        editor.display = show_editor
        editor.language = self.language
        self.query_one(OutputView).display = not is_markdown
        self.query_one(".cell-footer", Static).update(self.language)
        if show_editor:
            self._sync_editor_height()

    def _sync_editor_height(self) -> None:
        editor = self.query_one(TextArea)
        visual_line_count = editor.wrapped_document.height
        editor.styles.height = max(4, visual_line_count + 2)

    def set_running(self) -> None:
        self.query_one(".marker", Static).update("[*]")

    def set_result(
        self, result_outputs: list[object], execution_count: int | None
    ) -> None:
        self.model.outputs = result_outputs
        self.model.execution_count = execution_count
        self.query_one(OutputView).update_outputs(result_outputs)
        count = execution_count if execution_count is not None else "-"
        self.query_one(".marker", Static).update(f"[{count}]")

    def set_error(self) -> None:
        self.query_one(".marker", Static).update("[!]")


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

    def get_focused_code_cell(self) -> Cell | None:
        cell = self.get_focused_cell()
        if cell is None or cell.model.cell_type != "code":
            return None
        return cell

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
            focused_cell.exit_edit()
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
            focused_cell.exit_edit()
            await self.mount(cell, before=focused_cell)

        cell.focus()

    def edit_focused_cell(self) -> None:
        cell = self.get_focused_cell()
        if cell is not None:
            cell.enter_edit()

    def navigate_focused_cell(self) -> None:
        cell = self.get_focused_cell()
        if cell is not None:
            cell.exit_edit()
            cell.focus()

    def toggle_focused_cell_type(self) -> None:
        cell = self.get_focused_cell()
        if cell is not None:
            cell.toggle_type()

    def focus_relative_cell(self, offset: int) -> None:
        cells = list(self.query(".cell"))
        if not cells:
            return

        focused_cell = self.get_focused_cell()
        if focused_cell is None:
            target_index = 0 if offset > 0 else len(cells) - 1
        else:
            focused_cell.exit_edit()
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
    CSS_PATH = Path(__file__).with_name("app.tcss")
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
        self.kernel = ProjectKernel(path or Path.cwd())
        self._cell_running = False

    BINDINGS = [
        Binding("b", "add_cell", "Add cell", priority=True),
        Binding("d", "delete_cell", "Delete cell", priority=True),
        Binding("j", "move_down", "Move to next cell", priority=True),
        Binding("k", "move_up", "Move to previous cell", priority=True),
        Binding("a", "add_cell_above", "Add cell above", priority=True),
        Binding("r", "run_cell", "Run cell", priority=True),
        Binding("m", "toggle_cell_type", "Switch markdown/python"),
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
            # Set this after focus processing, which may otherwise select the
            # whole value and replace the command prefix on the next keypress.
            self.call_after_refresh(lambda: setattr(command_bar, "cursor_position", 1))
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

    def action_toggle_cell_type(self) -> None:
        self.query_one(CellContainer).toggle_focused_cell_type()

    async def action_run_cell(self) -> None:
        """Execute the focused code cell in the persistent project kernel."""
        if self._cell_running:
            self.notify("A cell is already running", severity="warning")
            return

        cell = self.query_one(CellContainer).get_focused_cell()
        if cell is None:
            self.notify("No cell is focused", severity="warning")
            return
        if cell.model.cell_type != "code":
            self.notify("Only code cells can be executed", severity="warning")
            return

        self._cell_running = True
        cell.set_running()
        try:
            result = await self.kernel.execute(cell.model.source)
            cell.set_result(result.outputs, result.execution_count)
        except KernelExecutionError as exc:
            cell.set_error()
            self.notify(str(exc), severity="error", timeout=8)
        finally:
            self._cell_running = False

    async def on_unmount(self) -> None:
        await self.kernel.shutdown()

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
        type=Path,
        help="notebook to open or create",
    )
    args = parser.parse_args()

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
