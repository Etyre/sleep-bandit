"""
applog.py -- one-line timestamped logging for Sleep Bandit.

Under the com.elityre.sleep-bandit LaunchAgent both stdout and stderr land in
~/Library/Logs/sleep-bandit.log, so every line written here carries a timestamp
and is flushed at once (plain print() is block-buffered when not on a terminal,
which is how the log stayed empty for months while tracebacks piled up in
sleep-bandit.err.log).
"""

import sys
import time
import traceback


def log(msg):
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def log_exc(msg="unhandled error"):
    """Log the current exception with its traceback, every line timestamped."""
    log(msg)
    for line in traceback.format_exc().rstrip().splitlines():
        log("    " + line)
