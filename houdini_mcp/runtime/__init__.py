"""Runtime: dry-run / execute / logs (orchestrates Core via TCP)."""

from runtime.dryrun import dry_run
from runtime.executor import execute
from runtime.logger import get_logs

__all__ = ["dry_run", "execute", "get_logs"]
