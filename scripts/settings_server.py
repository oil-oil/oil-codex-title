"""只监听回环地址的本地设置页；密钥只写入用户环境，不回传或调用模型。"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit
import webbrowser

from codex_adapter import BackendError, worker_env
from oil_codex_title import atomic_json, load_config, model_config_changes, read_json, thread_lock, validate_config, DEFAULTS
from user_environment import persist_user_key, validate_key_input

ASSETS = Path(__file__).resolve().parents[1] / "assets/settings"


class ConfigConflict(ValueError):
    pass


def revision(root):
    path = root / "config.json"
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def settings_view(root):
    config = load_config(root)
    relay = config["relay"]
    return {"provider": config["provider"], "official": {"model": config["model"],
            "service_tier": "fast" if config["service_tier"] else "standard"},
            "relay": ({**relay, "service_tier": "fast" if relay.get("service_tier") else "standard"}
                      if relay else None), "config_path": str(root / "config.json"), "revision": revision(root),
            "api_key_present": bool(relay and worker_env().get(relay["api_key_env"], "").strip()),
            "key_management_supported": sys.platform == "win32", "show_last_user_time": config["show_last_user_time"]}


def save_settings(root, payload):
    fields = {"revision", "provider", "model", "service_tier", "base_url", "api_key_env", "show_last_user_time"}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise ValueError("设置仅接受模式、模型、档位、API 地址和密钥环境变量名")
    if not all(isinstance(payload.get(key), str) for key in ("revision", "provider", "model", "service_tier")):
        raise ValueError("设置字段缺失或格式错误")
    if payload["provider"] not in ("official", "relay"):
        raise ValueError("请选择官方或中转站")
    for key in ("base_url", "api_key_env"):
        if key in payload and not isinstance(payload[key], str):
            raise ValueError("API 地址和密钥环境变量名必须是文本")
    if "show_last_user_time" in payload and type(payload["show_last_user_time"]) is not bool:
        raise ValueError("显示最后发言时间必须是布尔值")
    with thread_lock(root, "config", wait_seconds=3) as acquired:
        if not acquired:
            raise BackendError("配置正在保存，请稍后重试")
        if payload["revision"] != revision(root):
            raise ConfigConflict("配置已被其他程序更新，请重新读取后再保存")
        config = load_config(root)
        changes = model_config_changes(config, read_json(root / "config.json"),
            provider=payload["provider"], model=payload["model"], service_tier=payload["service_tier"],
            base_url=payload.get("base_url"), api_key_env=payload.get("api_key_env"))
        if "show_last_user_time" in payload:
            changes["show_last_user_time"] = payload["show_last_user_time"]
        validate_config(DEFAULTS | changes)
        atomic_json(root / "config.json", changes)
        return settings_view(root)


def save_key(payload, writer):
    if not isinstance(payload, dict) or set(payload) != {"api_key_env", "api_key"}:
        raise ValueError("请提交密钥环境变量名和密钥")
    name, value = payload["api_key_env"], payload["api_key"]
    validate_key_input(name, value)
    writer(name, value)
    return {"status": "key_saved", "api_key_env": name, "api_key_present": True, "restart_required": True}


def create_server(root, port=0, *, key_writer=None):
    if not 0 <= port <= 65535:
        raise ValueError("端口必须在 0～65535 之间")
    token = secrets.token_urlsafe(32)
    key_writer = key_writer or persist_user_key

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            # 不记录页面入口令牌或请求内容。
            pass

        def send(self, code, data, content_type="application/json; charset=utf-8"):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, dict) else data
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, authenticated=False):
            expected = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if self.headers.get("Host") != expected or (origin and origin != "http://" + expected):
                self.send(403, {"message": "此设置页只允许本机入口访问"})
                return False
            if authenticated and not secrets.compare_digest(self.headers.get("X-Oil-Settings-Token", "").encode("utf-8"), token.encode("ascii")):
                self.send(403, {"message": "设置页入口已失效，请使用 settings 命令重新打开"})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.allowed(authenticated=path.startswith("/api/")):
                return
            try:
                if path == "/api/config":
                    # 与写入共用锁，避免读取旧配置却拿到新版本号。
                    with thread_lock(root, "config", wait_seconds=3) as acquired:
                        if not acquired:
                            raise BackendError("配置正在保存，请稍后重试")
                        self.send(200, settings_view(root))
                elif path in ("/", "/settings.js", "/settings.css"):
                    filename, content_type = {"/": ("index.html", "text/html; charset=utf-8"),
                        "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
                        "/settings.css": ("settings.css", "text/css; charset=utf-8")}[path]
                    self.send(200, (ASSETS / filename).read_bytes(), content_type)
                else:
                    self.send(404, {"message": "页面不存在"})
            except (ValueError, BackendError) as exc:
                self.send(400, {"message": str(exc)})
            except OSError:
                self.send(500, {"message": "无法读取设置，请检查配置文件和目录权限"})

        def do_PUT(self):
            if not self.allowed(authenticated=True):
                return
            if self.path not in ("/api/config", "/api/key"):
                self.send(404, {"message": "接口不存在"})
                return
            try:
                raw_length = self.headers.get("Content-Length", "0")
                if not raw_length.isdigit():
                    raise ValueError("请提交有效的 JSON 设置")
                length = int(raw_length)
                if not 0 < length <= 16384 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请提交有效的 JSON 设置")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send(200, save_key(payload, key_writer) if self.path == "/api/key" else save_settings(root, payload))
            except ConfigConflict as exc:
                self.send(409, {"message": str(exc)})
            except (ValueError, BackendError) as exc:
                message = "请提交有效的 JSON 设置" if isinstance(exc, (json.JSONDecodeError, UnicodeError)) else str(exc)
                self.send(400, {"message": message})
            except OSError:
                self.send(500, {"message": "无法保存用户环境变量，请检查当前用户权限"} if self.path == "/api/key"
                          else {"message": "无法保存设置，请检查目录权限"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.settings_url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    return server


def serve_settings(root, *, port=0, open_browser=True):
    with create_server(root, port) as server:
        print(json.dumps({"status": "settings_ready", "url": server.settings_url}, ensure_ascii=False), flush=True)
        if open_browser:
            webbrowser.open(server.settings_url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
    return 0
