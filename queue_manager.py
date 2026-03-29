"""
Simple in-memory job queue manager
"""

import threading
from typing import Optional, Dict


class JobQueue:
    def __init__(self):
        self._jobs: Dict[int, dict] = {}
        self._lock = threading.Lock()

    def add_job(self, user_id: int) -> str:
        import uuid
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[user_id] = {
                "job_id": job_id,
                "status": "starting",
                "pages_done": 0,
                "total_pages": 0,
                "cancelled": False
            }
        return job_id

    def has_active_job(self, user_id: int) -> bool:
        with self._lock:
            return user_id in self._jobs

    def get_job(self, user_id: int) -> Optional[dict]:
        with self._lock:
            return self._jobs.get(user_id)

    def cancel_job(self, user_id: int) -> bool:
        with self._lock:
            if user_id in self._jobs:
                self._jobs[user_id]["cancelled"] = True
                self._jobs[user_id]["status"] = "cancelling"
                return True
        return False

    def is_cancelled(self, user_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(user_id)
            return job.get("cancelled", False) if job else False

    def update_progress(self, user_id: int, pages_done: int, total: int):
        with self._lock:
            if user_id in self._jobs:
                self._jobs[user_id]["pages_done"] = pages_done
                self._jobs[user_id]["total_pages"] = total
                self._jobs[user_id]["status"] = "processing"

    def remove_job(self, user_id: int):
        with self._lock:
            self._jobs.pop(user_id, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._jobs)
