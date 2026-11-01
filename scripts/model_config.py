"""独立模型的官方/中转站配置；只保存密钥环境变量名。"""
import re
from urllib.parse import urlsplit


def validate_model_config(config):
    if config.get("provider", "official") not in ("official", "relay"):
        raise ValueError("provider 必须是 official 或 relay")
    relay = config.get("relay")
    if relay is None:
        if config.get("provider") == "relay":
            raise ValueError("中转站需要配置 base_url、api_key_env 和 model")
        return
    if not isinstance(relay, dict):
        raise ValueError("relay 必须是对象")
    if set(relay) - {"base_url", "api_key_env", "model", "service_tier"}:
        raise ValueError("relay 仅支持 base_url、api_key_env、model 和 service_tier；密钥请放入环境变量")
    for key in ("base_url", "api_key_env", "model"):
        if not isinstance(relay.get(key), str) or not relay[key].strip():
            raise ValueError(f"relay.{key} 不能为空")
        if relay[key] != relay[key].strip() or any(ord(c) < 32 for c in relay[key]):
            raise ValueError(f"relay.{key} 不能含首尾空白或控制字符")
    try:
        url = urlsplit(relay["base_url"])
        valid = (url.scheme in ("http", "https") and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query and not url.fragment
                 and not any(c.isspace() for c in relay["base_url"]))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("relay.base_url 必须是 HTTP(S) API 基础地址，不能包含账号、密码、查询参数或片段")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", relay["api_key_env"]):
        raise ValueError("relay.api_key_env 必须是环境变量名，不能填写密钥值")
    if relay.get("service_tier") not in (None, "priority"):
        raise ValueError("relay.service_tier 必须是 null（标准）或 priority（Fast）")


def effective_model_config(config):
    validate_model_config(config)
    if config.get("provider", "official") == "relay":
        relay = config["relay"]
        return {**config, "model": relay["model"], "service_tier": relay.get("service_tier")}
    return config
