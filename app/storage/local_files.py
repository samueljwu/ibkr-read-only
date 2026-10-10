"""Bounded retries for brief Windows file sharing locks."""
import time


def replace_with_retry(source, target):
    for attempt in range(5):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (2 ** attempt))
