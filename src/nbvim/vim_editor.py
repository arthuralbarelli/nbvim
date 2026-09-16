"""In-cell vim modes on top of Textual's TextArea."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from textual import events
from textual.document._document import Selection
from textual.message import Message
from textual.widgets import TextArea


class VimMode(Enum):
    INSERT = "INSERT"
    NORMAL = "NORMAL"
    VISUAL = "VISUAL"
    VISUAL_LINE = "VISUAL_LINE"


@dataclass
class YankRegister:
    text: str = ""
    linewise: bool = False


class VimTextArea(TextArea):
    """A TextArea with Insert, Normal, and Visual modes."""

    class ModeChanged(Message):
        def __init__(self, mode: VimMode) -> None:
            super().__init__()
            self.mode = mode

    class ExitToNavigation(Message):
        """Leave the cell editor and return to notebook navigation."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.vim_mode = VimMode.INSERT
        self._register = YankRegister()
        self._pending: str | None = None
        self._pending_timer = None

    def check_consume_key(self, key: str, character: str | None = None) -> bool:
        if self.vim_mode == VimMode.INSERT:
            if key == "escape":
                return True
            return super().check_consume_key(key, character)
        return True

    async def _on_key(self, event: events.Key) -> None:
        if self.vim_mode == VimMode.INSERT:
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                self.enter_normal()
                return
            await super()._on_key(event)
            return

        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self._clear_pending()
            if self.vim_mode == VimMode.NORMAL:
                self.post_message(self.ExitToNavigation())
            else:
                self.enter_normal()
            return
        self._handle_command_key(event)

    def enter_insert(self) -> None:
        self._clear_pending()
        self._set_mode(VimMode.INSERT)

    def enter_normal(self) -> None:
        self._clear_pending()
        self.selection = Selection.cursor(self.cursor_location)
        self._set_mode(VimMode.NORMAL)

    def enter_visual(self, *, linewise: bool) -> None:
        self._clear_pending()
        if linewise:
            self._set_mode(VimMode.VISUAL_LINE)
            self._snap_visual_line()
            return
        self._set_mode(VimMode.VISUAL)
        if self.selection.is_empty:
            self.selection = Selection(
                self.cursor_location, self.get_cursor_right_location()
            )

    def _set_mode(self, mode: VimMode) -> None:
        self.vim_mode = mode
        self.post_message(self.ModeChanged(mode))

    def _handle_command_key(self, event: events.Key) -> None:
        token = _command_token(event)
        visual = self.vim_mode in {VimMode.VISUAL, VimMode.VISUAL_LINE}

        if visual:
            self._handle_visual_key(token)
            return

        if self._pending is not None:
            operator = self._pending
            self._clear_pending()
            if token == operator:
                self._apply_line_operator(operator)
                return

        if token == "i":
            self.enter_insert()
        elif token == "a":
            self.action_cursor_right()
            self.enter_insert()
        elif token in {"h", "j", "k", "l"}:
            self._move(token, select=False)
        elif token == "0":
            self.action_cursor_line_start()
        elif token == "$":
            self.action_cursor_line_end()
        elif token == "x":
            self._yank_selection_or_char()
            self.action_delete_right()
        elif token in {"d", "c", "y"}:
            self._set_pending(token)
        elif token == "p":
            self._paste()
        elif token == "u":
            self.undo()
        elif token == "v":
            self.enter_visual(linewise=False)
        elif token == "V":
            self.enter_visual(linewise=True)

    def _handle_visual_key(self, token: str) -> None:
        if token == "escape":
            self.enter_normal()
            return
        if token in {"h", "j", "k", "l"}:
            self._move(token, select=True)
            return
        if token == "0":
            self.action_cursor_line_start(select=True)
            self._refresh_visual_shape()
            return
        if token == "$":
            self.action_cursor_line_end(select=True)
            self._refresh_visual_shape()
            return
        if token in {"d", "x"}:
            self._yank_current_selection()
            self._delete_current_selection()
            self.enter_normal()
            return
        if token == "y":
            self._yank_current_selection()
            self.enter_normal()
            return
        if token == "c":
            self._yank_current_selection()
            self._delete_current_selection()
            self.enter_insert()
            return
        if token == "p":
            self._paste()
            return
        if token == "v":
            self.enter_visual(linewise=False)
            return
        if token == "V":
            self.enter_visual(linewise=True)

    def _move(self, token: str, *, select: bool) -> None:
        actions = {
            "h": self.action_cursor_left,
            "j": self.action_cursor_down,
            "k": self.action_cursor_up,
            "l": self.action_cursor_right,
        }
        actions[token](select)
        if select:
            self._refresh_visual_shape()

    def _refresh_visual_shape(self) -> None:
        if self.vim_mode == VimMode.VISUAL_LINE:
            self._snap_visual_line()

    def _snap_visual_line(self) -> None:
        start, end = self.selection
        top = min(start[0], end[0])
        bottom = max(start[0], end[0])
        line_count = self.document.line_count
        if bottom + 1 < line_count:
            new_end = (bottom + 1, 0)
        else:
            new_end = (bottom, len(self.document.get_line(bottom)))
        if end[0] >= start[0]:
            self.selection = Selection((top, 0), new_end)
        else:
            self.selection = Selection(new_end, (top, 0))

    def _apply_line_operator(self, operator: str) -> None:
        row, _ = self.cursor_location
        line = self.document.get_line(row)
        yanked = line + "\n"
        if operator == "y":
            self._register = YankRegister(yanked, linewise=True)
            return
        self._register = YankRegister(yanked, linewise=True)
        if operator == "d":
            self.action_delete_line()
            return
        self.delete((row, 0), (row, len(line)), maintain_selection_offset=False)
        self.enter_insert()

    def _yank_selection_or_char(self) -> None:
        if self.selection.is_empty:
            row, column = self.cursor_location
            line = self.document.get_line(row)
            if column < len(line):
                self._register = YankRegister(line[column], linewise=False)
            return
        self._yank_current_selection()

    def _yank_current_selection(self) -> None:
        self._register = YankRegister(
            self.selected_text,
            linewise=self.vim_mode == VimMode.VISUAL_LINE,
        )

    def _delete_current_selection(self) -> None:
        start, end = self.selection
        start, end = sorted((start, end))
        if start == end:
            return
        self.delete(start, end, maintain_selection_offset=False)

    def _paste(self) -> None:
        text = self._register.text
        if not text:
            return
        if self.vim_mode in {VimMode.VISUAL, VimMode.VISUAL_LINE}:
            start, end = sorted(self.selection)
            self.replace(text, start, end, maintain_selection_offset=False)
            self.enter_normal()
            return
        if self._register.linewise:
            self._paste_linewise(text)
            return
        row, column = self.cursor_location
        line = self.document.get_line(row)
        insert_at = (row, column + 1) if column < len(line) else (row, column)
        self.insert(text, insert_at, maintain_selection_offset=False)

    def _paste_linewise(self, text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        row, _ = self.cursor_location
        if row + 1 < self.document.line_count:
            self.insert(text, (row + 1, 0), maintain_selection_offset=False)
            self.move_cursor((row + 1, 0))
            return
        line = self.document.get_line(row)
        self.insert(
            "\n" + text.rstrip("\n"), (row, len(line)), maintain_selection_offset=False
        )
        self.move_cursor((row + 1, 0))

    def _set_pending(self, operator: str) -> None:
        self._clear_pending()
        self._pending = operator
        self._pending_timer = self.set_timer(0.5, self._clear_pending)

    def _clear_pending(self) -> None:
        self._pending = None
        if self._pending_timer is not None:
            self._pending_timer.stop()
            self._pending_timer = None


def _command_token(event: events.Key) -> str:
    if event.key == "escape":
        return "escape"
    if event.character:
        return event.character
    return event.key
