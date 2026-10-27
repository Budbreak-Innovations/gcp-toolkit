#!/usr/bin/env python3
"""Delete objects and "folders" (prefixes) from Google Cloud Storage.

Paths are given as gs:// URIs, either on the command line, in one or more
text files (one path per line), or both:

    gcs_delete.py gs://my-bucket/reports/2024.csv gs://my-bucket/tmp/
    gcs_delete.py --from-file paths.txt --dry-run
    cat paths.txt | gcs_delete.py --from-file - --yes

Path semantics:
    gs://bucket/dir/     -> every object under the prefix "dir/".
    gs://bucket/name     -> the object "name" (if it exists) AND every object
                            under "name/", like `gcloud storage rm -r`.
                            Never matches siblings such as "name-old".
    gs://bucket[/]       -> refused unless --allow-bucket-root is given.
                            The bucket itself is never deleted.

Safety:
    * Everything is resolved and summarised before anything is deleted.
    * Interactive confirmation is required unless --yes is passed; without a
      TTY and without --yes the script aborts.
    * Each delete carries an if_generation_match precondition, so an object
      rewritten after it was listed is not deleted.

Authentication uses Application Default Credentials
(`gcloud auth application-default login`, or a service account).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass

import requests
from google.api_core import exceptions as gexc
from google.auth import exceptions as auth_exc
from google.cloud import storage

log = logging.getLogger("gcs_delete")

GS_SCHEME = "gs://"

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3

PROGRESS_INTERVAL_S = 10


class UsageError(Exception):
    """Invalid input supplied by the user."""


@dataclass(frozen=True)
class GcsPath:
    bucket: str
    name: str  # object name or prefix; "" means the bucket root

    @property
    def is_bucket_root(self) -> bool:
        return self.name == ""

    @property
    def is_prefix(self) -> bool:
        return self.name.endswith("/")

    def __str__(self) -> str:
        return f"{GS_SCHEME}{self.bucket}/{self.name}"


@dataclass(frozen=True, slots=True)
class Target:
    """A single object generation scheduled for deletion.

    Slotted because a run can hold millions of these.
    """

    bucket: str
    name: str
    generation: int
    size: int

    def __str__(self) -> str:
        return f"{GS_SCHEME}{self.bucket}/{self.name}#{self.generation}"


def parse_gcs_path(raw: str) -> GcsPath:
    path = raw.strip()
    if not path.startswith(GS_SCHEME):
        raise UsageError(f"not a gs:// URI: {raw!r}")
    rest = path[len(GS_SCHEME):]
    bucket, _, name = rest.partition("/")
    if not bucket:
        raise UsageError(f"missing bucket name: {raw!r}")
    # Collapse "gs://bucket//" style input to the bucket root rather than an
    # object literally named "/".
    if name.strip("/") == "":
        name = ""
    return GcsPath(bucket=bucket, name=name)


def read_path_file(path: str) -> Iterator[str]:
    """Yield paths from a text file ("-" for stdin).

    Blank lines and lines starting with '#' are ignored.
    """
    if path == "-":
        lines: Iterable[str] = sys.stdin
        yield from _clean_lines(lines)
        return
    try:
        with open(path, encoding="utf-8") as fh:
            yield from _clean_lines(fh)
    except OSError as e:
        raise UsageError(f"cannot read path file {path!r}: {e}") from e


def _clean_lines(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def collect_paths(cli_paths: list[str], path_files: list[str]) -> list[GcsPath]:
    """Parse and de-duplicate all input paths, preserving order."""
    raw: list[str] = list(cli_paths)
    for f in path_files:
        raw.extend(read_path_file(f))
    if not raw:
        raise UsageError("no paths given (pass paths and/or --from-file)")

    seen: dict[GcsPath, None] = {}
    for r in raw:
        seen.setdefault(parse_gcs_path(r), None)
    return list(seen)


def resolve(
    client: storage.Client, path: GcsPath, all_versions: bool
) -> Iterator[Target]:
    """Expand a path into the concrete object generations to delete."""
    bucket = client.bucket(path.bucket)

    def from_listing(prefix: str, exact: str | None = None) -> Iterator[Target]:
        for blob in client.list_blobs(bucket, prefix=prefix, versions=all_versions):
            if exact is not None and blob.name != exact:
                continue
            yield Target(path.bucket, blob.name, blob.generation, blob.size or 0)

    if path.is_bucket_root or path.is_prefix:
        yield from from_listing(path.name)
        return

    # Exact object (only that name, not "name-old" etc.) ...
    if all_versions:
        yield from from_listing(path.name, exact=path.name)
    else:
        blob = bucket.get_blob(path.name)
        if blob is not None:
            yield Target(path.bucket, blob.name, blob.generation, blob.size or 0)
    # ... plus everything under "name/".
    yield from from_listing(path.name + "/")


def plan(
    client: storage.Client,
    paths: list[GcsPath],
    all_versions: bool,
    allow_bucket_root: bool,
) -> tuple[list[Target], list[GcsPath]]:
    """Resolve all paths. Returns (targets, paths_that_matched_nothing)."""
    for p in paths:
        if p.is_bucket_root and not allow_bucket_root:
            raise UsageError(
                f"{p} would empty the whole bucket; "
                "pass --allow-bucket-root if that is really intended"
            )

    targets: dict[Target, None] = {}
    unmatched: list[GcsPath] = []
    for p in paths:
        log.debug("resolving %s", p)
        before = len(targets)
        found = False
        for t in resolve(client, p, all_versions):
            found = True
            targets.setdefault(t, None)
        if not found:
            unmatched.append(p)
        log.info("%s: %d new object(s)", p, len(targets) - before)
    return list(targets), unmatched


def delete_one(client: storage.Client, target: Target, all_versions: bool) -> None:
    bucket = client.bucket(target.bucket)
    if all_versions:
        # Delete that specific (possibly noncurrent) generation.
        bucket.blob(target.name, generation=target.generation).delete()
    else:
        # Only delete the live generation we listed; if it was overwritten in
        # the meantime the precondition fails and the new data is kept.
        bucket.blob(target.name).delete(if_generation_match=target.generation)


def execute(
    client: storage.Client,
    targets: list[Target],
    all_versions: bool,
    workers: int,
) -> int:
    """Delete targets concurrently. Returns the number of failures.

    At most `workers * 4` deletes are queued at any time, so memory stays flat
    for millions of objects and Ctrl+C stops promptly.
    """
    failures = 0
    done = 0
    total = len(targets)
    max_in_flight = workers * 4
    started = last_report = time.monotonic()

    def collect(fut: Future, t: Target) -> None:
        nonlocal failures, done, last_report
        done += 1
        try:
            fut.result()
            log.debug("deleted %s", t)
        except gexc.NotFound:
            # Already gone: the desired end state, not an error.
            log.warning("already deleted: %s", t)
        except gexc.PreconditionFailed:
            failures += 1
            log.error("skipped, object changed since listing: %s", t)
        except gexc.GoogleAPICallError as e:
            failures += 1
            log.error("failed to delete %s: %s", t, e)
        now = time.monotonic()
        if now - last_report >= PROGRESS_INTERVAL_S or done == total:
            last_report = now
            rate = done / max(now - started, 1e-9)
            eta = (total - done) / rate if rate else 0
            log.info("progress: %d/%d (%.0f/s, ~%s left)",
                     done, total, rate, human_duration(eta))

    pool = ThreadPoolExecutor(max_workers=workers)
    in_flight: dict[Future, Target] = {}
    try:
        for t in targets:
            if len(in_flight) >= max_in_flight:
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    collect(fut, in_flight.pop(fut))
            in_flight[pool.submit(delete_one, client, t, all_versions)] = t
        for fut in as_completed(in_flight):
            collect(fut, in_flight[fut])
    except KeyboardInterrupt:
        log.error("interrupted after %d/%d; re-run to finish (already "
                  "deleted objects are simply skipped)", done, total)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return failures


def size_connection_pool(client: storage.Client, workers: int) -> None:
    """Give the HTTP session one pooled connection per worker thread.

    requests keeps only 10 connections per host by default; with more workers
    the extras are discarded after each call ("Connection pool is full"), so
    every request pays for a new TLS handshake.
    """
    session = client._http  # no public API for this; widely used workaround
    if getattr(session, "is_mtls", False):
        return  # keep the mTLS adapter configured by google-auth
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=1, pool_maxsize=max(workers, 10)
    )
    session.mount("https://", adapter)


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        log.error("refusing to prompt without a TTY; pass --yes to confirm")
        return False
    try:
        answer = input(f"{prompt} [y/N]: ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", metavar="GS_URI",
                   help="gs://bucket/object or gs://bucket/folder/ to delete")
    p.add_argument("-f", "--from-file", action="append", default=[], metavar="FILE",
                   help="text file with one gs:// path per line ('#' comments "
                        "and blank lines ignored; '-' reads stdin). Repeatable.")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="list what would be deleted and exit")
    p.add_argument("-y", "--yes", action="store_true",
                   help="do not prompt for confirmation")
    p.add_argument("-a", "--all-versions", action="store_true",
                   help="also delete noncurrent generations (versioned buckets)")
    p.add_argument("--allow-bucket-root", action="store_true",
                   help="allow gs://bucket/ which deletes every object in the bucket")
    p.add_argument("--project", help="GCP project for the client (default: from ADC)")
    p.add_argument("-j", "--workers", type=int, default=16,
                   help="concurrent delete requests (default: 16)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v to list every object, -vv for library debug output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if args.workers < 1:
        log.error("--workers must be >= 1")
        return EXIT_USAGE
    if "-" in args.from_file and not args.yes and not args.dry_run:
        log.error("reading paths from stdin requires --yes or --dry-run "
                  "(stdin cannot also be used for the confirmation prompt)")
        return EXIT_USAGE

    try:
        paths = collect_paths(args.paths, args.from_file)
        client = storage.Client(project=args.project)
        size_connection_pool(client, args.workers)
        targets, unmatched = plan(
            client, paths, args.all_versions, args.allow_bucket_root
        )
    except UsageError as e:
        log.error("%s", e)
        return EXIT_USAGE
    except auth_exc.DefaultCredentialsError as e:
        log.error("no credentials (try `gcloud auth application-default login`): %s", e)
        return EXIT_FAILURES
    except gexc.GoogleAPICallError as e:
        log.error("listing failed, nothing deleted: %s", e)
        return EXIT_FAILURES

    for p in unmatched:
        log.warning("no objects matched: %s", p)

    if not targets:
        log.info("nothing to delete")
        return EXIT_OK

    total_size = sum(t.size for t in targets)
    buckets = sorted({t.bucket for t in targets})
    summary = (f"{len(targets)} object(s), {human_bytes(total_size)}, "
               f"in bucket(s): {', '.join(buckets)}")

    if args.dry_run or args.verbose:
        for t in targets:
            print(f"{'would delete' if args.dry_run else 'delete'}: {t}")
    if args.dry_run:
        log.info("dry run: %s", summary)
        return EXIT_OK

    if not args.yes and not confirm(f"Permanently delete {summary}?"):
        log.info("aborted, nothing deleted")
        return EXIT_ABORTED

    failures = execute(client, targets, args.all_versions, args.workers)
    deleted = len(targets) - failures
    if failures:
        log.error("done: %d deleted, %d failed", deleted, failures)
        return EXIT_FAILURES
    log.info("done: %d deleted", deleted)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
