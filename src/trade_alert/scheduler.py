from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowSchedule:
    mode: str
    workflow: str
    hour: int
    minute: int
    label: str


WORKFLOW_SCHEDULES = (
    WorkflowSchedule("premarket", "premarket-alert.yml", 7, 30, "장전 후보"),
    WorkflowSchedule("preopen", "preopen-alert.yml", 8, 55, "장전 중간확정"),
    WorkflowSchedule("confirmation", "confirmation-alert.yml", 9, 10, "장초 최종확인"),
)


def create_scheduler(dispatch: Callable[[str], None]):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover - depends on optional server extra
        raise RuntimeError("install the server dependencies with: pip install '.[server]'") from exc

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    def run(mode: str) -> None:
        try:
            dispatch(mode)
            LOGGER.info("dispatched GitHub workflow for %s", mode)
        except Exception:
            LOGGER.exception("failed to dispatch GitHub workflow for %s", mode)

    for item in WORKFLOW_SCHEDULES:
        scheduler.add_job(
            run,
            CronTrigger(
                day_of_week="mon-fri",
                hour=item.hour,
                minute=item.minute,
                timezone="Asia/Seoul",
            ),
            args=(item.mode,),
            id=f"dispatch-{item.mode}",
            name=f"{item.hour:02d}:{item.minute:02d} {item.label}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
    return scheduler
