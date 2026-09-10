"""Notebook data models backed by the Jupyter ``nbformat`` schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import nbformat
from nbformat import NotebookNode


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

    @classmethod
    def from_nbformat(cls, cell: NotebookNode) -> "CellModel":
        return cls(
            cell_type=cell.cell_type,
            source=cell.get("source", ""),
            metadata=dict(cell.get("metadata", {})),
            outputs=list(cell.get("outputs", [])),
            execution_count=cell.get("execution_count"),
        )

    def to_nbformat(self) -> NotebookNode:
        if self.cell_type == "code":
            return nbformat.v4.new_code_cell(
                source=self.source,
                metadata=self.metadata,
                outputs=self.outputs,
                execution_count=self.execution_count,
            )
        if self.cell_type == "markdown":
            return nbformat.v4.new_markdown_cell(
                source=self.source, metadata=self.metadata
            )
        if self.cell_type == "raw":
            return nbformat.v4.new_raw_cell(source=self.source, metadata=self.metadata)
        raise ValueError(f"Unsupported cell type: {self.cell_type!r}")


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

    def __iter__(self) -> Iterable[CellModel]:
        return iter(self.cells)

    def __len__(self) -> int:
        return len(self.cells)
