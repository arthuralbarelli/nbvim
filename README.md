# nbvim

Vim-inspired terminal UI for editing and running Jupyter `.ipynb` notebooks.

Open or create a notebook, move between cells with vim-like keys, and run Python in your active environment.

![nbvim rendering a notebook image output](assets/nbvim-image-demo.gif)

![nbvim demo](assets/demo.gif)

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

The kernel uses the interpreter from `--python`, or else the active environment (`VIRTUAL_ENV`, then `CONDA_PREFIX`). It never uses the interpreter running nbvim, and it does not guess a project `.venv`. If nothing is active, the editor still opens and running a cell reports an error.

PNG, JPEG, GIF, and WebP display outputs are rendered in the terminal using
colored Unicode blocks. Images are scaled to the available output width, so
plots such as `matplotlib` figures can be viewed without leaving nbvim.

Try the included demo with:

```bash
uv run nbvim demo.ipynb
```

The target environment needs `ipykernel` installed.

```bash
source .venv/bin/activate
nbvim path/to/notebook.ipynb

nbvim --python /path/to/python path/to/notebook.ipynb
```

## Develop

```bash
make format   # Black
make lint     # Black --check
```
