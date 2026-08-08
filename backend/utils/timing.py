import time
import logging

logger = logging.getLogger("medcare.perf")

class Timer:
    """Context manager for timing code blocks."""
    def __init__(self, stage_name: str, log_level=logging.INFO):
        self.stage = stage_name
        self.log_level = log_level
        self.elapsed_ms = 0
    
    def __enter__(self):
        self.start = time.perf_counter()
        return self
    
    def __exit__(self, *args):
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000
        logger.log(self.log_level, f"[PERF] {self.stage}: {self.elapsed_ms:.1f}ms")
