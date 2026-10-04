"""Finite, safe config diagnostics. Never interpolate provider output."""
from enum import StrEnum


class ConfigErrorCode(StrEnum):
    AMBIGUOUS_TARGET = "ambiguous_target"
    UNAUTHORIZED_GROUP = "unauthorized_group"
    INVALID_LOCAL_CONFIG = "invalid_local_config"
    INVALID_MODEL_SCHEMA = "invalid_model_schema"
    UNSAFE_MODEL_FIELDS = "unsafe_model_fields"
    UNSUPPORTED_CONFIG = "unsupported_config"
    PROVIDER_ERROR = "provider_error"


MESSAGES = {
    ConfigErrorCode.AMBIGUOUS_TARGET: "无法唯一确定目标群，请使用 /config <群号> <配置意图>。",
    ConfigErrorCode.UNAUTHORIZED_GROUP: "该群当前未授权采集。请先使用 /allow add <群号>，收到提案后 /confirm <id>。",
    ConfigErrorCode.INVALID_LOCAL_CONFIG: "配置字段格式不合法。可使用 mode inbox、alias <别名> 或 <字段> true/false。",
    ConfigErrorCode.INVALID_MODEL_SCHEMA: "没有理解你希望修改哪项群设置。可以尝试：“只总结”“加入收件箱”“重点关注”“不要自动下载附件”。",
    ConfigErrorCode.UNSAFE_MODEL_FIELDS: "解析结果包含不允许的配置字段，未创建提案。请仅描述群模式、别名或功能开关。",
    ConfigErrorCode.UNSUPPORTED_CONFIG: "当前 /config 仅支持群别名、模式和功能开关，不支持定时切换、自动到期或条件规则。请重新说明要立即应用的设置。",
    ConfigErrorCode.PROVIDER_ERROR: "配置解析服务暂时不可用，请稍后重试；也可使用 /config <群号> mode inbox 等明确命令。",
}


class ConfigParseError(ValueError):
    def __init__(self, code: ConfigErrorCode, *, group_id: int | None = None):
        self.code = code
        message = MESSAGES[code]
        if group_id is not None and code == ConfigErrorCode.UNAUTHORIZED_GROUP:
            message = message.replace("<群号>", str(group_id))
        super().__init__(message)
