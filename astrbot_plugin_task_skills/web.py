"""Dashboard-authenticated UI. No listener, credentials or external assets."""

import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit


CONFIG_KEYS = ("auto_learn", "provider_id", "max_skills", "cooldown_seconds",
               "shared_skills", "review_mode", "auto_publish")


def public_error(exc):
    # Only fixed messages are exposed; filesystem errors may contain private paths.
    return {
        "invalid review note": "批准失败：可选备注须为不含凭据的文本，最多 4000 字符。",
        "revision conflict": "版本冲突：技能已更新，请刷新并重新核对当前版本后操作。",
        "administrator approval required": "发布失败：当前版本尚未通过管理员审核，请先批准；备注非必填。",
        "native publishing unavailable in isolated mode": "发布不可用：请检查共享模式及原生发布器是否已初始化。",
        "foreign or modified native skill": "发布或撤回被拒绝：原生技能不属于本插件或已被人工修改，不能覆盖。",
        "foreign native files": "发布或撤回被拒绝：原生目录包含非本插件文件，不能覆盖。",
        "revision unavailable": "回滚失败：所选历史版本不存在，请刷新后重试。",
        "rename not supported": "保存失败：不可修改技能 name，请保留原名称。",
        "unknown action": "操作失败：未知按钮操作，请刷新页面并检查插件版本。",
    }.get(str(exc), str(exc) if isinstance(exc, ValueError) and str(exc) in (
        "配置字段不完整或包含未知字段", "开关配置必须是布尔值", "审核模式必须是 manual 或 auto",
        "技能容量上限必须是 1-1000 的整数", "学习冷却时间必须是 60-86400 秒的整数",
        "Provider ID 必须是最多 256 个字符的文本", "请求必须使用 JSON", "请求内容过大")
          else "操作无效：请检查 JSON 字段、容量、文件归属及完整性；文件被人工修改时不会覆盖。")


def validate_config(payload):
    if not isinstance(payload, dict) or set(payload) != set(CONFIG_KEYS):
        raise ValueError("配置字段不完整或包含未知字段")
    if any(type(payload[key]) is not bool for key in ("auto_learn", "shared_skills", "auto_publish")):
        raise ValueError("开关配置必须是布尔值")
    if payload["review_mode"] not in ("manual", "auto"):
        raise ValueError("审核模式必须是 manual 或 auto")
    if type(payload["max_skills"]) is not int or not 1 <= payload["max_skills"] <= 1000:
        raise ValueError("技能容量上限必须是 1-1000 的整数")
    if type(payload["cooldown_seconds"]) is not int or not 60 <= payload["cooldown_seconds"] <= 86400:
        raise ValueError("学习冷却时间必须是 60-86400 秒的整数")
    if not isinstance(payload["provider_id"], str) or len(payload["provider_id"]) > 256:
        raise ValueError("Provider ID 必须是最多 256 个字符的文本")
    return dict(payload)


class SkillWeb:
    def __init__(self, plugin):
        self.plugin = plugin
        self.sessions = {}
        self.closed = False

    def close(self):
        self.closed = True
        self.sessions.clear()

    def authorize(self, username, method, headers, scheme):
        if self.closed or not isinstance(username, str) or not username.strip():
            raise PermissionError("需要登录 dashboard")
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if now - v[1] < 3600}
        if method == "POST":
            origin = urlsplit(headers.get("origin", ""))
            expected = headers.get("host", "")
            if (origin.scheme != scheme or origin.netloc != expected or origin.path
                    or origin.query or origin.fragment or not expected):
                raise PermissionError("来源地址被拒绝")
            current = self.sessions.get(username)
            if not current or not secrets.compare_digest(headers.get("x-task-csrf", ""), current[0]):
                raise PermissionError("CSRF 校验失败")
        if username not in self.sessions:
            if len(self.sessions) >= 128:
                raise PermissionError("管理会话数量已达上限")
            self.sessions[username] = (secrets.token_urlsafe(32), now)
        return self.sessions[username][0]

    async def page(self):
        from astrbot.api.web import request
        from starlette.responses import HTMLResponse

        self.authorize(request.username, "GET", request.headers, request._request.url.scheme)
        html = (Path(__file__).parent / "pages" / "技能管理" / "index.html").read_text(encoding="utf-8")
        html = html.replace('<script src="/api/plugin/page/bridge-sdk.js"></script>', "")
        return HTMLResponse(html,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})

    async def api(self):
        from astrbot.api.web import json_response, request

        try:
            bridge_path = "/api/v1/plugins/extensions/astrbot_plugin_task_skills/api"
            if request.path not in ("/api/plug/task-skills/api", bridge_path):
                raise PermissionError("仅允许通过 dashboard 管理页面访问")
            headers = dict(request.headers)
            if request.path == bridge_path:
                from astrbot.dashboard.api.auth import require_dashboard_user
                from astrbot.dashboard.responses import ApiError

                # The bridge uses v1 routes, whose default auth also accepts API keys.
                try:
                    username = await require_dashboard_user(request._request)
                except ApiError as exc:
                    raise PermissionError("需要登录 dashboard") from exc
                if username != request.username:
                    raise PermissionError("管理身份不匹配")
            if request.method not in ("GET", "POST"):
                raise PermissionError("仅允许 GET 或 POST")
            if request.method == "GET":
                payload = {}
            else:
                if request.content_type != "application/json":
                    raise ValueError("请求必须使用 JSON")
                if int(request.headers.get("content-length", "0")) > 16000:
                    raise ValueError("请求内容过大")
                raw = await request.body()
                if len(raw) > 16000:
                    raise ValueError("请求内容过大")
                payload = json.loads(raw)
                if request.path == bridge_path and isinstance(payload, dict):
                    # Official apiPost carries a JSON body, not custom headers.
                    headers["x-task-csrf"] = payload.pop("csrf", "")
            csrf = self.authorize(request.username, request.method, headers, request._request.url.scheme)
            result = self.dispatch(payload)
            result["csrf"] = csrf
            return json_response(result, headers={"Cache-Control": "no-store"})
        except PermissionError:
            return json_response({"error": "身份认证、来源地址或 CSRF 校验失败，请登录后刷新页面。"}, status_code=403)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return json_response({"error": public_error(exc)}, status_code=400)

    def dispatch(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        plugin = self.plugin
        shared = plugin.shared()
        scope = plugin.store.scope("shared", "shared")
        action = payload.get("action", "list")
        if action == "config":
            values = validate_config(payload.get("config"))
            old = {key: plugin.config.get(key, {
                "auto_learn": True, "provider_id": "", "max_skills": 100,
                "cooldown_seconds": 600, "shared_skills": True,
                "review_mode": "manual", "auto_publish": False}[key]) for key in CONFIG_KEYS}
            old_shared = shared
            plugin.config.save_config(values)
            try:
                if values["shared_skills"] != old_shared:
                    plugin.set_shared(values["shared_skills"])
                plugin.store.max_skills = values["max_skills"]
                plugin.store.state(scope, {"auto_learn": values["auto_learn"]})
            except Exception:
                plugin.config.save_config(old)
                raise
            shared = plugin.shared()
        elif action == "shared":
            plugin.set_shared(payload["enabled"])
            shared = plugin.shared()
        elif not shared and action not in ("list",):
            # A dashboard account has no chat session/sender identity. Never
            # expose isolated scopes through a global UI or accept arbitrary hashes.
            raise PermissionError("隔离模式请使用聊天管理指令")
        elif action == "edit":
            plugin.store.edit(scope, payload["name"], payload["skill"], payload["revision"])
        elif action == "delete":
            plugin.store.read(scope, payload["name"])
            if plugin.publisher:
                plugin.publisher.unpublish(payload["name"])
            plugin.store.delete(scope, payload["name"])
        elif action == "publish":
            plugin.publish(scope, payload["name"])
        elif action in ("approve", "approve_publish", "reject"):
            plugin.review(scope, payload["name"], action != "reject", payload.get("verification", ""),
                          action == "approve_publish", payload["revision"])
        elif action == "rollback":
            plugin.store.rollback(scope, payload["name"], int(payload["revision"]))
        elif action == "learning":
            if type(payload["enabled"]) is not bool:
                raise ValueError("invalid switch")
            plugin.store.state(scope, {"auto_learn": payload["enabled"]})
        elif action != "list":
            raise ValueError("unknown action")
        records = plugin.store.list(scope) if shared else []
        config = {key: plugin.config.get(key, {
            "auto_learn": True, "provider_id": "", "max_skills": 100,
            "cooldown_seconds": 600, "shared_skills": True,
             "review_mode": "manual", "auto_publish": False}[key]) for key in CONFIG_KEYS}
        config["shared_skills"] = shared
        config["auto_learn_default"] = plugin.config.get("auto_learn", True)
        state = plugin.store.state(scope)
        config["auto_learn_override"] = state.get("auto_learn")
        config["auto_learn"] = plugin.enabled(scope)
        return {"records": records, "stats": {"skills": len(records),
                "revisions": sum(len(r.get("history", [])) + 1 for r in records)},
                "config": config,
                 "notice": "当前为隔离模式：请使用聊天指令管理自己的技能。" if not shared else ("审核模式为自动发布：仅最终验证成功且技能校验通过的候选会自动批准并发布，请确认生成内容和安全过滤仍符合你的管理要求。" if config["review_mode"] == "auto" else "审核模式为人工审核：新生成、合并、编辑与回滚均保持待审核，不检索、不发布。可直接批准，备注非必填；批准不代表已实际测试。旧版无审核字段默认待审核，已发布原生文件不擅自删除。待审或拒绝更新不会影响已批准旧版。")}
