import os
import time

MAX_LOG_BYTES = 1 << 20


def _rotate(path, max_bytes):
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


def log_fn(path, max_bytes=MAX_LOG_BYTES):
    """Return a log(line) function appending timestamped lines to path.

    Rotates the file to <path>.1 once it exceeds max_bytes.
    Never raises — logging must not take the app down.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass

    def log(line):
        _rotate(path, max_bytes)
        try:
            with open(path, "a") as f:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write("[%s] %s\n" % (stamp, line))
        except OSError:
            pass

    return log


def append_line(path, line):
    """One-off append to a log file, with rotation. Never raises."""
    log_fn(path)(line)
