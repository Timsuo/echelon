PRIORITY_ICON = {'critical': '🚨', 'high': '🟠', 'normal': '🔵', 'low': '⚪'}
CATEGORY_ICON = {'announcement': '📢', 'schedule': '📅', 'schedule_change': '📅', 'location_change': '📍',
    'assignment': '📋', 'deadline': '⏰', 'exam': '🎓', 'material': '📎', 'administrative': '📢',
    'discussion': '💬', 'project': '🛠️', 'action_request': '📋', 'other': '💬'}
LABEL_ICON = {'deadline': '⏰', 'schedule_change': '📅', 'location_change': '📍', 'file': '📎', 'action_required': '📋'}
STATUS_ICON = {'likely_covered': '✅', 'partial': '⚠️', 'unknown': '❓', 'failed': '❌',
               'pending': '🔄', 'running': '🔄', 'archived': '🗂️', 'removed': '➖'}
SUMMARY_ICON = '📝'
COVERAGE_WARNING = '⚠️ 数据覆盖提示：该时间段存在未确认采集缺口，可能缺少后续修正或上下文。'


def plain(value: str) -> str:
    """Model text cannot supply UI emoji; preserve ordinary language/punctuation."""
    return ''.join(c for c in value if not (0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF
                                           or ord(c) in {0xFE0F, 0x200D, 0x20E3}))
