# nbvim

Vim-inspired terminal UI for editing and running Jupyter `.ipynb` notebooks.

Open or create a notebook, move between cells with vim-like keys, and run Python in the notebook's project environment.

![nbvim rendering a notebook image output](assets/nbvim-image-demo.gif)

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)

## Install and run

```bash
uv sync
uv run nbvim path/to/notebook.ipynb
```

If the path does not exist, nbvim creates a new notebook there before the UI starts.

## Keybindings

Bindings apply in navigation mode.

| Key | Action |
| --- | --- |
| `j` / `k` | Next / previous cell |
| `a` / `b` | Add cell above / after |
| `dd` | Delete cell (press `d` twice) |
| `Enter` | Edit cell |
| `Esc` | Return to navigation |
| `r` | Run the focused code cell |
| `m` | Switch cell between markdown and Python |
| `:` | Open the command bar |

## Commands

| Command | Action |
| --- | --- |
| `:w` | Save |
| `:q` | Quit without saving |
| `:wq` | Save and quit |
| `:q!` | Quit without saving |

Leaving the UI any other way saves the notebook automatically.

## Kernel

`r` executes the focused code cell in a persistent Jupyter Python kernel.

PNG, JPEG, GIF, and WebP display outputs are rendered in the terminal using
colored Unicode blocks. Images are scaled to the available output width, so
plots such as `matplotlib` figures can be viewed without leaving nbvim.

Try the included demo with:

```bash
uv run nbvim demo.ipynb
```

nbvim walks up from the notebook path looking for `pyproject.toml` or `.venv`, then uses that project's Python. If no project environment is found, it falls back to the interpreter running nbvim.

The project environment needs `ipykernel` installed.

## Develop

```bash
make format   # Black
make lint     # Black --check
```
