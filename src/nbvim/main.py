import argparse
import base64
import binascii
import hashlib
from io import BytesIO
from pathlib import Path

import nbformat
from PIL import Image, UnidentifiedImageError

# Query TGP/Sixel support before Textual starts I/O threads.
import textual_image.renderable  # noqa: F401
from textual.app import App, ComposeResult
from textual.events import Key
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Markdown, Static, TextArea
from textual_image.widget import Image as _AutoImage
from textual_image.widget._base import Image as _BaseImage

from .kernel import KernelExecutionError, ProjectKernel
from .model import CellModel, NotebookModel
from .vim_editor import VimTextArea


class OutputImage(_BaseImage, Renderable=_AutoImage._Renderable):
    """An image widget that reuses its terminal-side render across repaints.

    ``textual_image``'s ``Image.render()`` unconditionally discards and
    recreates its renderable on every call. For terminal graphics protocols
    (Kitty TGP, Sixel) that means re-encoding and re-transmitting the full
    image payload on every incidental repaint (scrolling past the cell,
    a sibling cell resizing, focus changes, ...), not just when the image or
    its rendered size actually changes. For a session with several
    retina-resolution matplotlib figures this shows up as image cells that
    are slow to render, or that never finish rendering because a later
    repaint interrupts an in-flight transmission.

    Skip the recreate-and-retransmit when neither the image nor its styled
    size has changed since the last render.
    """

    def render(self) -> object:
        if not self._image:
            return ""
        size = self._get_styled_size()
        if (
            self._renderable is not None
            and getattr(self, "_rendered_size", None) == size
        ):
            return self._renderable
        self._rendered_size = size
        return super().render()


# How long to wait between checks of the open notebook. Short enough that an
# edit saved from another pane shows up while you watch, long enough that a
# notebook with images is not re-read on every frame.
_RELOAD_POLL_SECONDS = 0.5
_UNREADABLE_RETRIES = 2


def _disk_signature(path: Path) -> tuple[int, int] | None:
    """Return ``(mtime_ns, size)`` for ``path``, or None if it cannot be stat'ed."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _file_digest(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _text_value(value: object) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value)


_IMAGE_MAGIC = (
    b"\x89PNG",
    b"\xff\xd8",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
)


def _image_bytes_from_data(value: object) -> bytes | None:
    """Normalize a Jupyter image payload to raw image bytes."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        if value.startswith(_IMAGE_MAGIC):
            return value
        try:
            return base64.b64decode(value)
        except binascii.Error:
            return value

    encoded = _text_value(value).strip()
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        return base64.b64decode(encoded)
    except (ValueError, binascii.Error):
        return None


def _image_from_data(value: object) -> Image.Image | None:
    """Decode a Jupyter image payload from base64, bytes, or a data URI."""
    raw = _image_bytes_from_data(value)
    if raw is None:
        return None
    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except (ValueError, UnidentifiedImageError, OSError):
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


def render_output(output: object) -> str | Image.Image:
    """Convert a notebook output into text or a decoded image."""
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
                        return image
    return format_output(output)


def _has_visible_output(outputs: list[object]) -> bool:
    """True when an output would draw text or an image in the cell."""
    return any(render_output(output) != "" for output in outputs)


class OutputView(Vertical):
    """Render the outputs currently stored on a cell."""

    def __init__(self, outputs: list[object] | None = None, **kwargs) -> None:
        self.outputs = outputs or []
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield from self._output_widgets()

    def _output_widgets(self) -> list[Widget]:
        widgets: list[Widget] = []
        for output in self.outputs:
            item = render_output(output)
            if item == "":
                continue
            if isinstance(item, Image.Image):
                widgets.append(OutputImage(item, classes="cell-output-image"))
            else:
                widgets.append(Static(item, markup=False, classes="cell-output-text"))
        return widgets

    async def update_outputs(self, outputs: list[object]) -> None:
        """Replace the rendered outputs, removing old widgets before mounting new ones.

        Awaiting both steps (inside a batch update) matters for image outputs:
        image widgets negotiate terminal graphics state (e.g. Kitty image IDs) on
        mount, and mounting a new image before the old one's cleanup has actually
        run can leave that state inconsistent, so the new image is slow to appear
        or never renders at all.
        """
        self.outputs = outputs
        widgets = self._output_widgets()
        async with self.batch():
            await self.remove_children()
            if widgets:
                await self.mount(*widgets)

    def clear_outputs(self) -> None:
        """Synchronously drop all outputs, e.g. when a cell stops being code."""
        self.outputs = []
        self.remove_children()


class Cell(Horizontal):
    """A focusable cell with a marker, editor, and language footer.

    In the notebook's preview view the editor, marker, and footer stay
    hidden. Markdown stays rendered and a code cell shows only its outputs.
    """

    can_focus = True

    class EditFinished(Message):
        """The cell left edit mode and is back in navigation."""

        def __init__(self, cell_id: str, source: str) -> None:
            super().__init__()
            self.cell_id = cell_id
            self.source = source

    def __init__(
        self,
        model: CellModel | None = None,
        language: str = "python",
        **kwargs,
    ) -> None:
        kwargs.setdefault("classes", "notebook-cell")
        super().__init__(**kwargs)
        self.model = model or CellModel()
        self.language = "markdown" if self.model.cell_type == "markdown" else language
        self._editing = False

    def compose(self) -> ComposeResult:
        is_markdown = self.model.cell_type == "markdown"
        markdown = Markdown(self.model.source, classes="cell-markdown")
        markdown.display = is_markdown
        editor = VimTextArea.code_editor(
            text=self.model.source,
            language=self.language,
        )
        editor.display = not is_markdown
        output = OutputView(self.model.outputs, classes="cell-output")
        output.display = not is_markdown
        # Blank row used when preview has a code cell with nothing to show.
        quiet = Static("", classes="cell-quiet", markup=False)
        quiet.display = False
        yield Static(self._bracket_marker(), classes="marker")
        with Vertical(classes="cell-editor"):
            yield markdown
            yield editor
            yield output
            yield quiet
            yield Static(self.language, classes="cell-footer")

    def on_mount(self) -> None:
        self._update_marker()
        self.apply_presentation()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Keep the notebook model and cell height in sync with the editor."""
        self.model.source = event.text_area.text
        visual_line_count = event.text_area.wrapped_document.height
        event.text_area.styles.height = max(4, visual_line_count + 2)

    def enter_edit(self) -> None:
        self._editing = True
        editor = self.query_one(VimTextArea)
        editor.enter_insert()
        self.apply_presentation()
        editor.focus()

    def exit_edit(self) -> None:
        if not self._editing:
            self.apply_presentation()
            return
        self._editing = False
        source = self.query_one(VimTextArea).text
        self.apply_presentation()
        self.post_message(self.EditFinished(self.model.id or "", source))

    def on_vim_text_area_mode_changed(self, event: VimTextArea.ModeChanged) -> None:
        """Keep the footer in sync with Insert / Normal / Visual."""
        self.query_one(".cell-footer", Static).update(self._footer_text())
        event.stop()

    def on_vim_text_area_exit_to_navigation(
        self, event: VimTextArea.ExitToNavigation
    ) -> None:
        """Esc in Normal mode returns to cell navigation."""
        self.exit_edit()
        self.focus()
        event.stop()

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
            self.query_one(OutputView).clear_outputs()
        self.query_one(".marker", Static).update(self._bracket_marker())
        self._editing = False
        self.apply_presentation()

    def _in_preview(self) -> bool:
        """True when the notebook is in the reading view and this cell is not open."""
        return getattr(self.app, "view_mode", "edit") == "preview" and not self._editing

    def apply_presentation(self) -> None:
        """Show edit chrome, or a reading view of the finished cell.

        Edit shows the code editor (and rendered markdown when not editing).
        Preview keeps markdown rendered and shows code outputs only. Assigning
        ``TextArea.language`` always rebuilds the document, so it is set only
        when the language actually changes.
        """
        is_markdown = self.model.cell_type == "markdown"
        markdown = self.query_one(".cell-markdown", Markdown)
        editor = self.query_one(TextArea)
        output = self.query_one(OutputView)
        footer = self.query_one(".cell-footer", Static)
        marker = self.query_one(".marker", Static)
        quiet = self.query_one(".cell-quiet", Static)

        if self._in_preview():
            markdown.display = is_markdown
            if markdown.display:
                markdown.update(self.model.source)
            editor.display = False
            footer.display = False
            marker.display = False
            show_output = not is_markdown and _has_visible_output(self.model.outputs)
            output.display = show_output
            quiet.display = not is_markdown and not show_output
            return

        show_editor = not is_markdown or self._editing
        markdown.display = is_markdown and not self._editing
        if markdown.display:
            markdown.update(self.model.source)
        editor.display = show_editor
        if editor.language != self.language:
            editor.language = self.language
        output.display = not is_markdown
        footer.display = True
        footer.update(self._footer_text())
        marker.display = True
        quiet.display = False
        if show_editor:
            self._sync_editor_height()

    def _footer_text(self) -> str:
        if self._editing:
            mode = self.query_one(VimTextArea).vim_mode.value
            return f"{self.language}  -- {mode} --"
        return self.language

    def _sync_editor_height(self) -> None:
        editor = self.query_one(TextArea)
        visual_line_count = editor.wrapped_document.height
        editor.styles.height = max(4, visual_line_count + 2)

    def _bracket_marker(self) -> str:
        """Idle prompt beside the cell. Markdown cells have no bracket marker."""
        if self.model.cell_type == "markdown":
            return ""
        return "[ ]"

    def _update_marker(self) -> None:
        """Show the execution count, or the idle prompt for a cell that has not run."""
        if self.model.cell_type == "code" and self.model.execution_count is not None:
            label = f"[{self.model.execution_count}]"
        else:
            label = self._bracket_marker()
        self.query_one(".marker", Static).update(label)

    async def adopt_model(self, model: CellModel, *, keep_editor_text: bool) -> None:
        """Point this widget at ``model`` and refresh outputs and source.

        ``keep_editor_text`` is set for the cell currently being edited. The
        editor text, cursor, and vim mode stay as the user left them.
        ``apply_presentation`` is skipped in that case: assigning
        ``TextArea.language`` rebuilds the document and jumps the cursor to
        the start, even when the language did not change.
        """
        editor = self.query_one(VimTextArea)
        if keep_editor_text:
            model.source = editor.text
        source_changed = not keep_editor_text and editor.text != model.source
        outputs_changed = self.model.outputs != model.outputs
        self.model = model
        self.language = "markdown" if model.cell_type == "markdown" else "python"
        if source_changed:
            editor.load_text(model.source)
        output = self.query_one(OutputView)
        if model.cell_type == "code":
            if outputs_changed:
                await output.update_outputs(model.outputs)
        elif output.outputs:
            output.clear_outputs()
        self._update_marker()
        if keep_editor_text:
            return
        self.apply_presentation()

    def set_running(self) -> None:
        self.query_one(".marker", Static).update("[*]")

    async def set_result(
        self, result_outputs: list[object], execution_count: int | None
    ) -> None:
        self.model.outputs = result_outputs
        self.model.execution_count = execution_count
        await self.query_one(OutputView).update_outputs(result_outputs)
        count = execution_count if execution_count is not None else "-"
        self.query_one(".marker", Static).update(f"[{count}]")
        self.apply_presentation()

    def set_error(self) -> None:
        self.query_one(".marker", Static).update("[!]")


class CellContainer(VerticalScroll):
    """Container responsible for displaying and editing notebook cells."""

    def __init__(self, notebook: NotebookModel | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.notebook = notebook or NotebookModel.new()
        self._clipboard: CellModel | None = None

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
        cells = list(self.query(".notebook-cell"))
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
        cells = list(self.query(".notebook-cell"))
        if cell is None or cell not in cells or len(cells) == 1:
            return

        cell_index = cells.index(cell)
        next_index = min(cell_index + 1, len(cells) - 1)
        if next_index == cell_index:
            next_index -= 1

        cells[next_index].focus()
        self.notebook.remove_cell(self.notebook.cells.index(cell.model))
        cell.remove()

    def copy_focused_cell(self) -> bool:
        cell = self.get_focused_cell()
        if cell is None:
            return False
        self._clipboard = cell.model.clone()
        return True

    async def paste_after_focused(self) -> bool:
        if self._clipboard is None:
            return False
        focused_cell = self.get_focused_cell()
        model = self._clipboard.clone()
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
        return True

    def focused_is_last(self) -> bool:
        cells = list(self.query(".notebook-cell"))
        focused = self.get_focused_cell()
        return bool(cells) and focused is cells[-1]

    def editing_cell(self) -> Cell | None:
        for cell in self.query(".notebook-cell"):
            if isinstance(cell, Cell) and cell._editing:
                return cell
        return None

    async def sync_from_disk(
        self,
        loaded: NotebookModel,
        *,
        preserve: tuple[str, str] | None = None,
    ) -> bool:
        """Rebuild the open notebook from ``loaded``.

        Cells are matched by id, so an external insert, delete, or reorder
        updates the widgets in place. When a cell is being edited, its editor
        text is kept and the other cells still reload. ``preserve`` does the
        same for a cell that just left edit mode, but lets a type change from
        disk through.

        Returns True when the edited cell's type on disk was held back so the
        editor could stay open. The caller should apply that type after the
        user returns to navigation.
        """
        editing = self.editing_cell()
        cursor = None
        type_held = False
        if editing is not None:
            editor = editing.query_one(VimTextArea)
            cursor = editor.cursor_location
            type_held = self._keep_edit(loaded, editing)

        if preserve is not None:
            self._keep_source(loaded, preserve[0], preserve[1])

        if self._notebook_matches(loaded):
            if editing is not None and cursor is not None:
                self._restore_edit_focus(editing, cursor)
            return type_held

        focus = self.get_focused_cell()
        focus_id = focus.model.id if focus is not None else None
        focus_index = (
            self.notebook.cells.index(focus.model)
            if focus is not None and focus.model in self.notebook.cells
            else 0
        )
        scroll_y = self.scroll_y

        existing: dict[str, Cell] = {}
        for cell in self.query(".notebook-cell"):
            if (
                isinstance(cell, Cell)
                and cell.model.id
                and cell.model.id not in existing
            ):
                existing[cell.model.id] = cell

        ordered: list[Cell] = []
        seen: set[str] = set()
        for model in loaded.cells:
            if not model.id or model.id in seen:
                continue
            seen.add(model.id)
            widget = existing.get(model.id)
            keep_text = widget is not None and widget is editing
            if widget is None or (
                widget.model.cell_type != model.cell_type and not keep_text
            ):
                widget = self.create_cell(model)
            else:
                await widget.adopt_model(model, keep_editor_text=keep_text)
            ordered.append(widget)

        self.notebook.cells = [cell.model for cell in ordered]
        self.notebook.metadata = dict(loaded.metadata)
        async with self.batch():
            await self._arrange(ordered)

        if editing is not None:
            kept = next(
                (cell for cell in ordered if cell.model.id == editing.model.id), None
            )
            if kept is not None and cursor is not None:
                self._restore_edit_focus(kept, cursor)
            return type_held

        self._restore_navigation_focus(ordered, focus_id, focus_index)
        self.scroll_to(0, scroll_y, animate=False)
        return False

    def _keep_edit(self, loaded: NotebookModel, editing: Cell) -> bool:
        """Overwrite the disk copy of the focused cell with the unsaved edit.

        Returns True when the file changed this cell's type and that change
        was held back so the open editor stays put.
        """
        editor = editing.query_one(VimTextArea)
        source = editor.text
        old_index = (
            self.notebook.cells.index(editing.model)
            if editing.model in self.notebook.cells
            else 0
        )
        match = next(
            (cell for cell in loaded.cells if cell.id == editing.model.id), None
        )
        if match is None:
            editing.model.source = source
            loaded.cells.insert(min(old_index, len(loaded.cells)), editing.model)
            return False
        held_type = match.cell_type != editing.model.cell_type
        if held_type:
            # Switching type now would close the editor under the caret.
            # Hold the current type and apply the disk type once edit ends.
            match.cell_type = editing.model.cell_type
            match.outputs = editing.model.outputs
            match.execution_count = editing.model.execution_count
        match.source = source
        return held_type

    def _keep_source(self, loaded: NotebookModel, cell_id: str, source: str) -> None:
        """Keep ``source`` on ``cell_id`` without pinning its cell type."""
        match = next((cell for cell in loaded.cells if cell.id == cell_id), None)
        if match is not None:
            match.source = source
            return
        current = next(
            (
                cell
                for cell in self.query(".notebook-cell")
                if isinstance(cell, Cell) and cell.model.id == cell_id
            ),
            None,
        )
        if current is None:
            loaded.cells.insert(0, CellModel(id=cell_id, source=source))
            return
        current.model.source = source
        old_index = (
            self.notebook.cells.index(current.model)
            if current.model in self.notebook.cells
            else 0
        )
        loaded.cells.insert(min(old_index, len(loaded.cells)), current.model)

    def _notebook_matches(self, loaded: NotebookModel) -> bool:
        current_ids = [cell.model.id for cell in self.query(".notebook-cell")]
        loaded_ids = [cell.id for cell in loaded.cells]
        if current_ids != loaded_ids or self.notebook.metadata != loaded.metadata:
            return False
        widgets = {
            cell.model.id: cell
            for cell in self.query(".notebook-cell")
            if isinstance(cell, Cell)
        }
        for model in loaded.cells:
            widget = widgets.get(model.id)
            if widget is None or not widget.model.same_payload(model):
                return False
        return True

    async def _arrange(self, ordered: list[Cell]) -> None:
        incoming = [cell for cell in ordered if cell.parent is not self]
        if incoming:
            await self.mount(*incoming)
        keep = set(ordered)
        for child in list(self.children):
            if child not in keep:
                await child.remove()
        for index, cell in enumerate(ordered):
            if index >= len(self.children):
                break
            if index == 0:
                if self.children[0] is not cell:
                    self.move_child(cell, before=0)
                continue
            if self.children.index(cell) != index:
                self.move_child(cell, after=ordered[index - 1])

    def _restore_edit_focus(self, cell: Cell, cursor: tuple[int, int]) -> None:
        editor = cell.query_one(VimTextArea)
        if editor.cursor_location != cursor:
            editor.move_cursor(cursor)
        if self.app.focused is not editor:
            editor.focus()

    def _restore_navigation_focus(
        self, ordered: list[Cell], focus_id: str | None, focus_index: int
    ) -> None:
        if not ordered:
            return
        if focus_id is not None:
            for cell in ordered:
                if cell.model.id == focus_id:
                    cell.focus()
                    return
        ordered[max(0, min(focus_index, len(ordered) - 1))].focus()


class NbVim(App):
    CSS_PATH = Path(__file__).with_name("app.tcss")
    # Tests stretch this so a timer cannot reload the notebook mid-assertion.
    RELOAD_INTERVAL = _RELOAD_POLL_SECONDS
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
        self._notebook_watch = None
        self._disk_signature: tuple[int, int] | None = None
        self._disk_digest: str | None = None
        self._failed_signature: tuple[int, int] | None = None
        self._failed_retries = 0
        self._polling = False
        self._reload_pending = False
        self._resync_edited_cell = False
        self._force_disk_read = False
        self._preserve_source: tuple[str, str] | None = None
        # "edit" is the working notebook. "preview" is the reading view.
        self.view_mode = "edit"

    BINDINGS = [
        Binding("b", "add_cell", "Add cell"),
        Binding("d", "delete_cell", "Delete cell"),
        Binding("j", "move_down", "Move to next cell"),
        Binding("k", "move_up", "Move to previous cell"),
        Binding("a", "add_cell_above", "Add cell above"),
        Binding("c", "copy_cell", "Copy cell"),
        Binding("v", "paste_cell", "Paste cell"),
        Binding("r", "run_cell", "Run cell and go to next"),
        Binding("shift+r", "run_cell_stay", "Run cell and stay"),
        Binding("m", "toggle_cell_type", "Switch markdown/python"),
        Binding("p", "toggle_preview", "Toggle preview"),
        Binding("enter", "edit_cell", "Edit cell"),
        Binding("escape", "navigate_cell", "Navigate cells"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(self.view_mode, id="mode-status")
        yield CellContainer(self.notebook, id="cells")
        yield Input(id="command-bar")

    def on_mount(self) -> None:
        # Textual's default auto-focus lands on the CellContainer itself
        # (it's a focusable VerticalScroll), not on the first Cell. That
        # leaves get_focused_cell() returning None until something explicitly
        # focuses a cell, so the very first j/k/r press on a fresh notebook
        # does nothing. Focus the first cell directly so navigation and
        # running work immediately.
        self._capture_disk_snapshot()
        if self.path is not None:
            self._notebook_watch = self.set_interval(
                self.RELOAD_INTERVAL,
                self._poll_notebook_file,
                name="notebook-file-watch",
            )
        self.query_one(CellContainer).focus_relative_cell(1)

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
        elif command == "preview":
            self.action_toggle_preview()
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
        # Remember the bytes we just wrote. The watcher compares digests, so
        # this save (and a later timestamp-only touch of the same bytes) does
        # not reload the UI and cannot loop.
        self._capture_disk_snapshot()

    def _capture_disk_snapshot(self) -> None:
        if self.path is None:
            return
        signature = _disk_signature(self.path)
        digest = _file_digest(self.path)
        if signature is None or digest is None:
            return
        self._disk_signature = signature
        self._disk_digest = digest
        self._failed_signature = None
        self._failed_retries = 0

    async def _poll_notebook_file(self) -> None:
        """Reload the open notebook when another process writes it."""
        if self.path is None or self._polling:
            return
        self._polling = True
        try:
            await self._poll_notebook_file_body()
        finally:
            self._polling = False

    async def _poll_notebook_file_body(self) -> None:
        path = self.path
        if path is None:
            return
        signature = _disk_signature(path)
        if signature is None:
            return
        retrying = (
            signature == self._failed_signature
            and self._failed_retries < _UNREADABLE_RETRIES
        )
        unchanged = (
            signature == self._disk_signature
            and not self._force_disk_read
            and not retrying
        )
        if unchanged:
            return
        if self._cell_running and not self._force_disk_read:
            # The running cell widget has to stay mounted until set_result.
            # Leave the snapshot where it is so the next poll still sees this
            # write, and apply it when the run finishes.
            self._reload_pending = True
            return
        digest = _file_digest(path)
        if digest is None:
            return
        if digest == self._disk_digest and not self._force_disk_read:
            self._disk_signature = signature
            self._failed_signature = None
            self._failed_retries = 0
            return
        try:
            loaded = NotebookModel.load(path)
        except (OSError, ValueError, nbformat.NBFormatError):
            # A writer may still be mid-save. Retry a couple of times, then
            # wait until the signature changes again.
            if signature != self._failed_signature:
                self._failed_signature = signature
                self._failed_retries = 1
            else:
                self._failed_retries += 1
            return
        self._failed_signature = None
        self._failed_retries = 0
        self._disk_signature = signature
        self._disk_digest = digest
        self._force_disk_read = False
        preserve = self._preserve_source
        self._preserve_source = None
        await self._apply_disk_notebook(loaded, preserve=preserve)

    async def _apply_disk_notebook(
        self,
        loaded: NotebookModel,
        *,
        preserve: tuple[str, str] | None = None,
    ) -> None:
        container = self.query_one(CellContainer)
        type_held = await container.sync_from_disk(loaded, preserve=preserve)
        if type_held:
            self._resync_edited_cell = True

    async def on_cell_edit_finished(self, event: Cell.EditFinished) -> None:
        """Apply a type change that was held back while the cell was edited."""
        if not self._resync_edited_cell:
            return
        self._resync_edited_cell = False
        self._force_disk_read = True
        self._preserve_source = (event.cell_id, event.source)
        await self._poll_notebook_file()

    def _is_editing(self) -> bool:
        return isinstance(self.focused, TextArea)

    async def action_add_cell(self) -> None:
        if self._is_editing():
            return
        await self.query_one(CellContainer).add_cell_after_focused()

    def action_move_down(self) -> None:
        if self._is_editing():
            return
        self.query_one(CellContainer).focus_relative_cell(1)

    def action_move_up(self) -> None:
        if self._is_editing():
            return
        self.query_one(CellContainer).focus_relative_cell(-1)

    async def action_add_cell_above(self) -> None:
        if self._is_editing():
            return
        await self.query_one(CellContainer).add_cell_above_focused()

    def action_copy_cell(self) -> None:
        if self._is_editing():
            return
        self.query_one(CellContainer).copy_focused_cell()

    async def action_paste_cell(self) -> None:
        if self._is_editing():
            return
        pasted = await self.query_one(CellContainer).paste_after_focused()
        if not pasted:
            self.notify("Nothing to paste", severity="warning")

    def action_edit_cell(self) -> None:
        if self._is_editing():
            return
        # Leave the reading view first so Enter still opens the vim editor.
        if self.view_mode == "preview":
            self._set_view_mode("edit")
        self.query_one(CellContainer).edit_focused_cell()

    def action_toggle_preview(self) -> None:
        """Switch between the edit notebook and the reading preview.

        Preview hides code source and vim chrome. It does not enter vim
        insert; that still happens from the edit view with Enter.
        """
        if self._is_editing():
            return
        self._set_view_mode("edit" if self.view_mode == "preview" else "preview")

    def _set_view_mode(self, mode: str) -> None:
        self.view_mode = mode
        self.query_one("#mode-status", Static).update(mode)
        for cell in self.query(Cell):
            cell.apply_presentation()

    def action_navigate_cell(self) -> None:
        self.query_one(CellContainer).navigate_focused_cell()

    def action_toggle_cell_type(self) -> None:
        if self._is_editing():
            return
        self.query_one(CellContainer).toggle_focused_cell_type()

    async def action_run_cell(self) -> None:
        """Run the focused cell, then move to the next one."""
        if await self._execute_focused_cell():
            await self._advance_after_run()

    async def action_run_cell_stay(self) -> None:
        """Run the focused cell and keep focus where it is."""
        await self._execute_focused_cell()

    async def _execute_focused_cell(self) -> bool:
        """Execute the focused code cell. Return True when advancing is allowed."""
        if self._is_editing():
            return False
        if self._cell_running:
            self.notify("A cell is already running", severity="warning")
            return False

        cell = self.query_one(CellContainer).get_focused_cell()
        if cell is None:
            self.notify("No cell is focused", severity="warning")
            return False
        if cell.model.cell_type != "code":
            return True

        self._cell_running = True
        cell.set_running()
        try:
            result = await self.kernel.execute(cell.model.source)
            await cell.set_result(result.outputs, result.execution_count)
            return True
        except KernelExecutionError as exc:
            cell.set_error()
            self.notify(str(exc), severity="error", timeout=8)
            return False
        finally:
            self._cell_running = False
            if self._reload_pending:
                self._reload_pending = False
                self.call_later(self._poll_notebook_file)

    async def _advance_after_run(self) -> None:
        container = self.query_one(CellContainer)
        if container.focused_is_last():
            await container.add_cell_after_focused()
            return
        container.focus_relative_cell(1)

    async def on_unmount(self) -> None:
        await self.kernel.shutdown()

    def action_delete_cell(self) -> None:
        """Delete the focused cell after two consecutive presses of ``d``."""
        if self._is_editing():
            return
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
