CSS = """
#cells {
    height: 1fr;
}

.cell {
    height: auto;
    margin-bottom: 1;
    border: solid transparent;
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
    width: 1fr;
    height: 4;
    min-height: 4;
    scrollbar-size: 0 0;
}
"""
