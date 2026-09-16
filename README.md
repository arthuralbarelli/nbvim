# nbvim

Vim-inspired terminal UI for editing and running Jupyter `.ipynb` notebooks.

Open or create a notebook, move between cells with vim-like keys, and run Python in your active environment.

![nbvim rendering a notebook image output](assets/nbvim-image-demo.gif)

![nbvim demo](assets/demo.gif)

## Demo

A short recording of real usage against the [analysis-test](https://github.com/arthuralbarelli/analysis-test) QA notebook: opening the notebook, navigating cells with `j`/`k` (including past a markdown table), running cells with `r` through a persistent kernel (version output, a pandas table, a rendered matplotlib chart), editing a cell with vim motions (`i`/`Esc`, `yy`, `p`, `dd`), and saving with `:w`.

<video src="assets/nbvim-demo.mp4" controls muted playsinline width="800">
Your browser does not support the video tag. <a href="assets/nbvim-demo.mp4">Download the demo video</a>.
</video>

If the video above doesn't play inline, [download it directly](assets/nbvim-demo.mp4).

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

Bindings apply in navigation mode unless noted.

| Key | Action |
| --- | --- |
| `j` / `k` | Next / previous cell |
| `a` / `b` | Add cell above / after |
| `c` | Copy the focused cell |
| `v` | Paste the copied cell below |
| `dd` | Delete cell (press `d` twice) |
| `Enter` | Edit cell (starts in Insert) |
| `Esc` | Insert → Normal; Normal → navigation |
| `r` | Run the focused code cell and go to the next cell |
| `R` | Run the focused code cell and stay |
| `m` | Switch cell between markdown and Python |
| `:` | Open the command bar |

`r` on the last cell inserts a new code cell below and focuses it. A failed run stays on the current cell. On markdown, `r` / `R` skip the kernel: `r` still advances, `R` stays.

### Edit mode

The cell editor uses vim Insert, Normal, and Visual modes.

| Key | Action |
| --- | --- |
| `i` / `a` | Insert at cursor / after cursor |
| `h` `j` `k` `l` | Move (extend the selection in Visual) |
| `0` / `$` | Line start / end |
| `x` | Delete the character under the cursor |
| `dd` / `cc` / `yy` | Delete / change / yank the current line |
| `p` | Paste the in-cell register |
| `u` | Undo |
| `v` / `V` | Character / line Visual |

In Visual, `d`/`x` delete the selection, `y` yanks it, and `c` deletes it and enters Insert.

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

PNG, JPEG, GIF, and WebP display outputs are rendered with the terminal's
native graphics protocol when available (Kitty TGP or Sixel). Terminals without
those protocols fall back to colored Unicode blocks. Images fill the available
output width. matplotlib figures are captured at retina resolution.

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
