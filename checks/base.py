from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class CheckStatus(str, Enum):
    PASSED   = "passed"
    FAILED   = "failed"
    ADVISORY = "advisory"
    SKIPPED  = "skipped"
    ERROR    = "error"


@dataclass
class CheckResult:
    check_id:   str
    status:     CheckStatus
    detail:     str
    blocking:   bool = True
    metadata:   dict[str, Any] = field(default_factory=dict)
    timestamp:  str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def passed(self) -> bool:
        return self.status == CheckStatus.PASSED

    @property
    def failed(self) -> bool:
        return self.status in (CheckStatus.FAILED, CheckStatus.ERROR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id":  self.check_id,
            "status":    self.status.value,
            "detail":    self.detail,
            "blocking":  self.blocking,
            "metadata":  self.metadata,
            "timestamp": self.timestamp,
        }


class BaseCheck:
    """
    All checks inherit from this. Subclasses implement `run()`.
    The orchestrator calls `execute()` which wraps `run()` with
    top-level error handling so an unhandled exception in any check
    never crashes the whole worker task.
    """

    check_id: str = "base_check"
    blocking: bool = True

    def run(self, **kwargs: Any) -> CheckResult:
        raise NotImplementedError

    def execute(self, **kwargs: Any) -> CheckResult:
        try:
            return self.run(**kwargs)
        except Exception as exc:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.ERROR,
                detail=f"Unexpected error in {self.check_id}: {exc!r}",
                blocking=self.blocking,
                metadata={"exception_type": type(exc).__name__},
            )
