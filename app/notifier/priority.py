from enum import IntEnum


class OutboxPriority(IntEnum):
    BACKGROUND = 40
    SUMMARY = 50
    SCHEDULED_DIGEST = 60
    MANUAL_DIGEST = 70
    INTERACTIVE = 80
    RECOVERY = 85
    HIGH = 90
    CRITICAL = 100


AGING_SECONDS = 60
AGING_CAP = int(OutboxPriority.CRITICAL - OutboxPriority.BACKGROUND)
