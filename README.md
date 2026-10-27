# gcp-toolkit

Small utilities for working with Google Cloud Platform.

## Setup (once)

```bash
uv sync                                  # create .venv
gcloud auth application-default login    # authenticate
```

## Running long jobs (screen)

Run long scripts inside `screen` so they survive closing the terminal or losing SSH
(install once with `sudo apt install screen`).

1. **Start a named session** that logs all output:
   ```bash
   screen -S myjob -L -Logfile ~/myjob_$(date +%Y%m%d_%H%M).log
   ```
2. **Set up the shell** inside the session:
   ```bash
   deactivate 2>/dev/null; cd ~/Documents/repo/gcp-toolkit
   ```
3. **Run the script with the laptop kept awake:**
   ```bash
   systemd-inhibit --what=sleep:idle --why="myjob" uv run ./<script>.py ...
   ```
4. Answer any prompts, then **detach** with `Ctrl+A`, `D`.

| Action | Command |
|--------|---------|
| Reattach | `screen -r myjob` |
| List sessions | `screen -ls` |
| Watch the log | `tail -f ~/myjob_*.log` |
| Close (after the job finishes) | `exit` inside the session |

Keep the laptop on power, lid open, and online. Don't press `Ctrl+C` in the session unless you
want to stop the job.

## Scripts

### `gcs_delete.py` — delete files and folders from Cloud Storage

Deletes `gs://` paths given as arguments or listed in a `.txt` file (one per line, `#` comments ok).
`gs://b/dir/` deletes everything inside `dir/`. `gs://b/name` deletes the file `name` and everything
inside `name/`.

```bash
uv run ./gcs_delete.py -f paths.txt --dry-run > dryrun_list.txt   # preview, deletes nothing
uv run ./gcs_delete.py -f paths.txt -j 64                         # delete (asks to confirm)
```

Options: `-j N` parallel workers (default 16), `-a` include old versions, `-y` skip the prompt,
`-v` print every object. See `--help` for the full list. If a run is interrupted, run it again
and it deletes only what's left. With soft delete on (`budprod`: 7 days), restore with
`gcloud storage restore "gs://bucket/path/**"`.

## Tests

```bash
uv run python -m unittest discover -s tests
```
