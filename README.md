# gcp-toolkit

Small utilities for working with Google Cloud Platform.

## `gcs_delete.py` — delete files and folders from Cloud Storage

Deletes `gs://` paths given on the command line and/or listed in a `.txt` file.

### Setup (once)

```bash
uv sync                                  # create .venv
gcloud auth application-default login    # authenticate
```

### Steps

1. **Write the paths file**, one `gs://` path per line (blank lines and `#` comments are ignored):
   ```text
   gs://my-bucket/some/folder/
   gs://my-bucket/report.csv
   ```
2. **Dry run.** This shows what would be deleted and deletes nothing:
   ```bash
   uv run ./gcs_delete.py -f paths.txt --dry-run > dryrun_list.txt
   ```
3. **Run the delete.** For large deletes, do this inside `screen`
   (see [Running long jobs](#running-long-jobs-screen)):
   ```bash
   systemd-inhibit --what=sleep:idle --why="GCS delete" \
     uv run ./gcs_delete.py -f paths.txt -j 64
   ```
4. **Wait for the confirmation prompt.** Listing comes first and can take minutes. Then it asks:
   ```
   Permanently delete 1594663 object(s), 53.5 TiB, in bucket(s): budprod? [y/N]:
   ```
   Check the numbers match the dry run, then type `y`. Anything else cancels.
   Progress is printed every 10 s until `done:`.

If the run is interrupted, run the same command again. It deletes only what's left.

### What a path deletes

| Path | Deletes |
|------|---------|
| `gs://b/dir/` | everything inside `dir/` |
| `gs://b/name` | file `name` **and** everything inside `name/` (never `name-old` etc.) |
| `gs://b/` | refused unless `--allow-bucket-root` (the bucket itself is never deleted) |

### Arguments

| Argument | Meaning |
|----------|---------|
| `GS_URI ...` | paths to delete |
| `-f, --from-file FILE` | text file of paths (`-` = stdin); can repeat |
| `-n, --dry-run` | list what would be deleted, delete nothing |
| `-y, --yes` | skip the confirmation prompt |
| `-j, --workers N` | parallel deletes (default 16; use ~64 for large jobs) |
| `-a, --all-versions` | also delete old versions (versioned buckets) |
| `--allow-bucket-root` | allow emptying a whole bucket |
| `--project` | GCP project (default: from credentials) |
| `-v` | print every object |

### Notes

- **Needs internet** for the whole run. Short drops are retried automatically.
- **Speed:** one run does about 250 deletes/s (Python is the bottleneck, not `-j`). For millions of
  objects, split the paths file (`split -n l/4 -d paths.txt part_`) and run one copy per part,
  each in its own `screen` session.
- **Soft delete:** if the bucket has it enabled (`budprod` has 7 days), deleted files can be
  restored and are still billed for that time, then removed permanently.
  Restore with `gcloud storage restore "gs://bucket/path/**"`.
- **Cost:** delete requests are free; listing costs cents.

## Running long jobs (screen)

Use `screen` for any script that runs for a long time. The job keeps running if you close the
terminal or lose an SSH connection.

1. **Install (once):** `sudo apt install screen`
2. **Start a named session** that also saves all output to a log file:
   ```bash
   screen -S myjob -L -Logfile ~/myjob_$(date +%Y%m%d_%H%M).log
   ```
3. **Inside the new session, set up the shell.** It is a new shell (your `~/.bashrc` runs again),
   so make sure you are in the repo and no other virtualenv is active:
   ```bash
   deactivate 2>/dev/null; cd ~/Documents/repo/gcp-toolkit
   ```
4. **Run the command**, wrapped in `systemd-inhibit` so the laptop does not go to sleep
   (`screen` does not stop the job from pausing if the laptop suspends):
   ```bash
   systemd-inhibit --what=sleep:idle --why="myjob" uv run ./<script>.py ...
   ```
5. **Answer any prompts** (e.g. `[y/N]`) before you leave. A prompt waits until you reattach.
6. **Detach:** press `Ctrl+A`, then `D`. The job keeps running.

| Action | Command |
|--------|---------|
| Reattach | `screen -r myjob` |
| List sessions | `screen -ls` |
| Watch the log from another terminal | `tail -f ~/myjob_*.log` |
| Close the session (after the job finishes) | `exit` (inside the session) |

Do not press `Ctrl+C` inside the session unless you want to stop the job. Keep the laptop on
power, with the lid open, and connected to the network.

## Tests

```bash
uv run python -m unittest discover -s tests
```
