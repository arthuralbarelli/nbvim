"""Notebook data models backed by the Jupyter ``nbformat`` schema."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import nbformat
from nbformat import NotebookNode


def format_duration(seconds: float) -> str:
    """Format a cell run duration.

    Under 10 seconds, one decimal place (``1.2s``). Up to 60 seconds, whole
    seconds (``12s``). Above that, minutes and zero-padded seconds (``1m 05s``).
    """
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds <= 60:
        return f"{int(seconds)}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def _nbvim_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    current = metadata.get("nbvim")
    if isinstance(current, dict):
        return current
    current = {}
    metadata["nbvim"] = current
    return current


def _new_cell_id() -> str:
    """Return a notebook cell id nbformat will accept.

    Matches ``nbformat``'s own ids (8 hex characters) so reloads can match a
    cell in the UI to the same cell in the file.
    """
    return uuid.uuid4().hex[:8]


@dataclass
class CellModel:
    """A cell in a notebook.

    ``source`` is kept as a string because that is the most convenient form for
    the editor, while ``outputs`` and ``metadata`` are retained for round trips
    through an ``.ipynb`` file.
    """

    cell_type: str = "code"
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    outputs: list[Any] = field(default_factory=list)
    execution_count: int | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _new_cell_id()

    @classmethod
    def from_nbformat(cls, cell: NotebookNode) -> "CellModel":
        return cls(
            cell_type=cell.cell_type,
            source=cell.get("source", ""),
            metadata=dict(cell.get("metadata", {})),
            outputs=list(cell.get("outputs", [])),
            execution_count=cell.get("execution_count"),
            id=cell.get("id") or None,
        )

    def to_nbformat(self) -> NotebookNode:
        if self.cell_type == "code":
            return nbformat.v4.new_code_cell(
                source=self.source,
                metadata=self.metadata,
                outputs=self.outputs,
                execution_count=self.execution_count,
                id=self.id,
            )
        if self.cell_type == "markdown":
            return nbformat.v4.new_markdown_cell(
                source=self.source, metadata=self.metadata, id=self.id
            )
        if self.cell_type == "raw":
            return nbformat.v4.new_raw_cell(
                source=self.source, metadata=self.metadata, id=self.id
            )
        raise ValueError(f"Unsupported cell type: {self.cell_type!r}")

    def clone(self) -> "CellModel":
        """Return an independent copy of this cell, including outputs and metadata."""
        return CellModel(
            cell_type=self.cell_type,
            source=self.source,
            metadata=deepcopy(self.metadata),
            outputs=deepcopy(self.outputs),
            execution_count=self.execution_count,
        )

    def record_duration_s(self, duration_s: float) -> None:
        """Store a run duration measured by the kernel, in cell metadata."""
        _nbvim_metadata(self.metadata)["duration_ms"] = int(round(duration_s * 1000))

    def clear_duration(self) -> None:
        """Drop a stored run duration. Other metadata is left in place."""
        nbvim_meta = self.metadata.get("nbvim")
        if not isinstance(nbvim_meta, dict) or "duration_ms" not in nbvim_meta:
            return
        nbvim_meta.pop("duration_ms", None)
        if not nbvim_meta:
            self.metadata.pop("nbvim", None)

    def duration_s(self) -> float | None:
        """Return the stored run duration in seconds, if this cell has one."""
        nbvim_meta = self.metadata.get("nbvim")
        if not isinstance(nbvim_meta, dict):
            return None
        duration_ms = nbvim_meta.get("duration_ms")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
            return None
        return float(duration_ms) / 1000

    def set_source(self, source: str) -> bool:
        """Replace the source. A real edit clears the stored run duration.

        Return True when a duration was cleared.
        """
        if source == self.source:
            return False
        had_duration = self.duration_s() is not None
        self.source = source
        self.clear_duration()
        return had_duration

    def same_payload(self, other: "CellModel") -> bool:
        """Whether two cells would look the same in the notebook UI."""
        return (
            self.id == other.id
            and self.cell_type == other.cell_type
            and self.source == other.source
            and self.outputs == other.outputs
            and self.execution_count == other.execution_count
            and self.metadata == other.metadata
        )


@dataclass
class NotebookModel:
    """The editable notebook document used by the application."""

    cells: list[CellModel] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls) -> "NotebookModel":
        """Create a new notebook with the initial empty code cell."""
        return cls(cells=[CellModel()])

    @classmethod
    def from_nbformat(cls, notebook: NotebookNode) -> "NotebookModel":
        return cls(
            cells=[CellModel.from_nbformat(cell) for cell in notebook.cells],
            metadata=dict(notebook.get("metadata", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> "NotebookModel":
        """Load a notebook from disk and normalize it to nbformat v4."""
        return cls.from_nbformat(nbformat.read(path, as_version=4))

    def to_nbformat(self) -> NotebookNode:
        return nbformat.v4.new_notebook(
            cells=[cell.to_nbformat() for cell in self.cells],
            metadata=self.metadata,
        )

    def save(self, path: str | Path) -> None:
        """Write the current document as a valid ``.ipynb`` file."""
        nbformat.write(self.to_nbformat(), path)

    def add_cell(
        self, cell: CellModel | None = None, index: int | None = None
    ) -> CellModel:
        """Insert a cell and return it. By default, append it to the notebook."""
        cell = cell or CellModel()
        if index is None:
            self.cells.append(cell)
        else:
            self.cells.insert(index, cell)
        return cell

    def remove_cell(self, index: int) -> CellModel:
        return self.cells.pop(index)

    def same_payload(self, other: "NotebookModel") -> bool:
        """Whether two notebooks would look the same in the UI."""
        if self.metadata != other.metadata or len(self.cells) != len(other.cells):
            return False
        return all(
            left.same_payload(right) for left, right in zip(self.cells, other.cells)
        )

    def __iter__(self) -> Iterable[CellModel]:
        return iter(self.cells)

    def __len__(self) -> int:
        return len(self.cells)
