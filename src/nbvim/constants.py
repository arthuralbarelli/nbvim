CSS = """
#cells {
    height: 1fr;
}

.cell {
    height: auto;
    margin-bottom: 1;
    border: solid transparent;
}

.cell-editor {
    width: 1fr;
    height: auto;
}

.cell-footer {
    width: 100%;
    height: 1;
    text-align: right;
    padding-right: 1;
    color: $text-muted;
}

.cell:focus-within {
    border: solid white;
}

.marker {
    width: 5;
    min-width: 5;
    padding: 1 1;
}

TextArea {
    width: 100%;
    height: 4;
    min-height: 4;
    scrollbar-size: 0 0;
}
"""
