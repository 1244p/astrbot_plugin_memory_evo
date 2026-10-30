"""memory_evo：小规模长期记忆 + 人格演化。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:  # 4.29 之前的版本没有这个 helper，退回插件数据目录下的 config/
    from astrbot.core.utils.astrbot_path import get_astrbot_config_path
except ImportError:  # pragma: no cover - 仅老版本走到

    def get_astrbot_config_path() -> str:
        return os.path.join(get_astrbot_plugin_data_path(), "config")

from . import dashboard, extractor, persona, proactive, settings
from .store import MemoryStore

PLUGIN_NAME = "astrbot_plugin_memory_evo"

HELP_TEXT = (
    "/memory dump 查看记忆（⚠️ 表示待确认）\n"
    "/memory why <词> 看召回打分\n"
    "/memory confirm <id> 确认一条待确认记忆\n"
    "/memory reindex 补写缺失语义向量\n"
    "/memory care 主动消息 on|off|status|now\n"
    "/memory forget <id|词|all> 删除\n"
    "/memory persona 查看人格状态与演化记录"
)


class MemoryEvoPlugin(Star):
    """长期记忆 + 人格演化。只记住值得记的，人格只缓慢漂移。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        db_path = self._get("db_path", "") or os.path.join(
            get_astrbot_plugin_data_path(), PLUGIN_NAME, "memory.db"
        )
        self.store = MemoryStore(db_path)
        self.baseline = {k: float(self._get(f"baseline_{k}", 0.5)) for k in persona.TRAIT_KEYS}
        self.buffers: dict[str, deque[tuple[str, str, float]]] = {}
        self.inflight: set[str] = set()
        self._scopes: set[str] = set()
        self._warned: set[str] = set()
        self._ticks = 0
        self._emb_resolved = False
        self._emb_provider = None
        self._emb_model = ""
        self._janitor: asyncio.Task | None = None
        self._schema = settings.load_schema(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), settings.SCHEMA_FILENAME)
        )
        self._register_views()
        self._start_janitor()

    # ---------------- 基础设施 ----------------

    # ---------------- Dashboard 视图后端 ----------------

    def _register_views(self) -> None:
        """注册 WebUI 插件视图用的 API。老版本 AstrBot 没有 register_web_api，直接跳过。"""
        register = getattr(self.context, "register_web_api", None)
        if register is None:
            logger.debug("[memory_evo] 当前 AstrBot 不支持插件视图，跳过 Web API 注册")
            return
        routes = [
            ("scopes", self.api_scopes, ["GET"], "列出有记忆的会话"),
            ("overview", self.api_overview, ["GET"], "单个会话的记忆概览"),
            ("facts", self.api_facts, ["GET"], "分页列出事实"),
            ("facts/delete", self.api_delete_facts, ["POST"], "删除选定事实"),
            ("facts/confirm", self.api_confirm_facts, ["POST"], "确认待确认事实"),
            ("persona", self.api_persona, ["GET"], "人格状态与演化记录"),
            ("care", self.api_care, ["GET", "POST"], "主动消息状态与开关"),
            ("export", self.api_export, ["GET"], "导出会话记忆"),
            ("settings", self.api_settings, ["GET", "POST"], "读取与保存插件设置"),
        ]
        for path, handler, methods, desc in routes:
            try:
                register(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
            except Exception:
                logger.exception(f"[memory_evo] 注册视图 API 失败：{path}")

    async def api_scopes(self):
        from astrbot.api.web import json_response

        rows = await asyncio.to_thread(dashboard.list_scopes, self.store.conn)
        return json_response({"items": rows})

    def _query_scope(self) -> str:
        from astrbot.api.web import request

        scope = request.query.get("scope") or ""
        if not scope:
            scopes = dashboard.list_scopes(self.store.conn)
            scope = scopes[0]["scope"] if scopes else ""
        return scope

    async def api_overview(self):
        from astrbot.api.web import json_response

        scope = self._query_scope()
        if not scope:
            return json_response({"scope": "", "empty": True})
        data = await asyncio.to_thread(dashboard.overview, self.store.conn, scope)
        data["care"] = await asyncio.to_thread(self._care_block, scope)
        data["care"]["settings"] = self._care_cfg()
        data["persona"] = await asyncio.to_thread(
            dashboard.persona_block, self.store.conn, scope, self.baseline
        )
        return json_response(data)

    async def api_facts(self):
        from astrbot.api.web import json_response, request

        scope = self._query_scope()
        q = request.query.get("q", "") or ""
        kind = request.query.get("kind", "") or ""
        status = request.query.get("status", "") or ""
        limit = request.query.get("limit", 50, type=int) or 50
        offset = request.query.get("offset", 0, type=int) or 0
        data = await asyncio.to_thread(
            dashboard.list_facts, self.store.conn, scope, q, kind, status, limit, offset
        )
        return json_response(data)

    async def _body(self) -> dict:
        from astrbot.api.web import request

        payload = await request.json(default={})
        return payload if isinstance(payload, dict) else {}

    async def api_delete_facts(self):
        from astrbot.api.web import error_response, json_response

        payload = await self._body()
        scope = str(payload.get("scope") or "")
        ids = payload.get("ids") or []
        if not scope or not isinstance(ids, list):
            return error_response("需要 scope 与 ids", status_code=400)
        deleted = await asyncio.to_thread(
            dashboard.delete_many, self.store.conn, scope, ids
        )
        logger.info(f"[memory_evo] WebUI 删除 {deleted} 条记忆 scope={scope} ids={ids}")
        return json_response({"deleted": deleted})

    async def api_confirm_facts(self):
        from astrbot.api.web import error_response, json_response

        payload = await self._body()
        scope = str(payload.get("scope") or "")
        ids = payload.get("ids") or []
        if not scope or not isinstance(ids, list):
            return error_response("需要 scope 与 ids", status_code=400)
        changed = await asyncio.to_thread(
            dashboard.confirm_many, self.store.conn, scope, ids
        )
        return json_response({"confirmed": changed})

    async def api_persona(self):
        from astrbot.api.web import json_response

        scope = self._query_scope()
        data = await asyncio.to_thread(
            dashboard.persona_block, self.store.conn, scope, self.baseline
        )
        return json_response(data)

    async def api_care(self):
        from astrbot.api.web import error_response, json_response, request

        if request.method.upper() == "POST":
            payload = await self._body()
            scope = str(payload.get("scope") or "")
            if not scope or "enabled" not in payload:
                return error_response("需要 scope 与 enabled", status_code=400)
            await asyncio.to_thread(
                self.store.set_care_enabled, scope, bool(payload.get("enabled"))
            )
            return json_response(await asyncio.to_thread(self._care_block, scope))
        scope = self._query_scope()
        data = await asyncio.to_thread(self._care_block, scope)
        data["settings"] = self._care_cfg()
        return json_response(data)

    def _care_block(self, scope: str) -> dict:
        return dashboard.care_block(
            self.store.conn, scope, float(self._get("care_min_idle_minutes", 90))
        )

    async def api_settings(self):
        from astrbot.api.web import json_response, request

        if request.method.upper() == "POST":
            payload = await self._body()
            values = payload.get("values")
            if not isinstance(values, dict):
                values = payload
            clean, errors = await asyncio.to_thread(settings.validate, self._schema, values)
            if errors:
                # 有任何字段不合法就整体不落盘，前端按 key 标红。
                # 用 200 + ok:false 而不是 4xx：bridge 把非 2xx 直接 reject，前端拿不到字段级错误。
                return json_response({"ok": False, "errors": errors})
            changed = await asyncio.to_thread(self._save_settings, clean)
            data = await asyncio.to_thread(settings.describe, self._schema, dict(self.cfg))
            data.update(
                {
                    "ok": True,
                    "changed": changed,
                    "restart_required": sorted(set(changed) & settings.RESTART_FIELDS),
                }
            )
            return json_response(data)

        data = await asyncio.to_thread(settings.describe, self._schema, dict(self.cfg))
        data["ok"] = True
        return json_response(data)

    def _save_settings(self, values: dict) -> list[str]:
        """写回配置并让改动立刻生效。返回真正变化的字段名。"""
        changed = settings.diff(dict(self.cfg), values)
        if not changed:
            return []

        self.cfg.update(changed)  # self.cfg 就是 self.config，钩子下一轮即读到新值
        saver = getattr(self.cfg, "save_config", None)
        try:
            if callable(saver):
                saver()
            else:  # 测试替身或极老的框架：直接合并进配置文件
                settings.save_to_file(
                    os.path.join(get_astrbot_config_path(), f"{PLUGIN_NAME}_config.json"),
                    changed,
                )
        except Exception:
            logger.exception(f"[memory_evo] 保存设置失败，已改动：{sorted(changed)}")
            raise
        logger.info(f"[memory_evo] 设置已更新：{sorted(changed)}")

        if any(k.startswith("baseline_") for k in changed):
            self.baseline = {k: float(self._get(f"baseline_{k}", 0.5)) for k in persona.TRAIT_KEYS}
        if "embedding_provider_id" in changed:
            self._emb_resolved = False
            self._emb_provider = None
            self._emb_model = ""
        return sorted(changed)

    async def api_export(self):
        from astrbot.api.web import file_response, json_response

        scope = self._query_scope()
        if not scope:
            return json_response({"error": "no scope"})
        payload = await asyncio.to_thread(dashboard.export_payload, self.store.conn, scope)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        name = f"memory-{scope.replace(':', '_')[:60]}.json"

        def write() -> str:
            outdir = os.path.join(
                get_astrbot_plugin_data_path(), PLUGIN_NAME, "exports"
            )
            os.makedirs(outdir, exist_ok=True)
            path = os.path.join(outdir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return path

        # file_response 要的是磁盘路径，不是字节；导出文件落在插件数据目录里
        path = await asyncio.to_thread(write)
        return file_response(
            path, filename=name, content_type="application/json; charset=utf-8"
        )

    def _get(self, key: str, default):
        value = self.cfg.get(key, default) if hasattr(self.cfg, "get") else default
        return default if value is None else value

    def _start_janitor(self) -> None:
        if self._janitor and not self._janitor.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环（例如被同步方式实例化），留到第一次请求时再起
            self._janitor = None
            return
        self._janitor = loop.create_task(
            self._janitor_loop(), name=f"{PLUGIN_NAME}-janitor"
        )

    async def _janitor_loop(self) -> None:
        """空闲补写：对话停下来的 60s 后把缓冲落盘，避免只在轮数达标时才记忆。"""
        try:
            while True:
                await asyncio.sleep(20)
                idle = float(self._get("idle_seconds", 60))
                now = time.time()
                for scope, buf in list(self.buffers.items()):
                    if not buf:
                        continue
                    if now - buf[-1][2] >= idle:
                        await self._flush(scope, reason="idle")
                for scope in list(self._scopes):
                    try:
                        await self._backfill_embeddings(scope, limit=16)
                    except Exception:
                        logger.debug("[memory_evo] 向量补写失败 scope=%s", scope, exc_info=True)
                self._ticks += 1
                if self._ticks % 3 == 0:
                    await self._care_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[memory_evo] janitor 异常退出")

    async def _provider(self, umo: str, provider_id: str | None = None):
        if provider_id is None:
            provider_id = str(self._get("extract_provider_id", "") or "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
            self._warn_missing_provider("extract", provider_id)
        return await self.context.get_using_provider_async(umo)

    def _available_ids(self, embed: bool = False) -> list[str]:
        try:
            providers = (
                self.context.get_all_embedding_providers()
                if embed
                else self.context.get_all_providers()
            )
        except Exception:
            return []
        ids: list[str] = []
        for provider in providers or []:
            try:
                ids.append(str(provider.provider_config.get("id", "") or "?"))
            except Exception:
                ids.append("?")
        return ids

    def _warn_missing_provider(self, kind: str, pid: str, embed: bool = False) -> None:
        """配置的 provider ID 不存在时，只提醒一次，并列出真实可用的 ID。"""
        key = f"{kind}:{pid}"
        if key in self._warned:
            return
        self._warned.add(key)
        available = self._available_ids(embed)
        hint = "、".join(available) if available else "（当前没有任何可用的 provider）"
        fallback = "第一个可用 embedding" if embed else "当前会话模型"
        logger.warning(
            f"[memory_evo] {kind}_provider_id={pid!r} 不存在，已退回{fallback}。"
            f"可用 ID：{hint}"
        )

    def _embedder(self):
        """解析 embedding provider：显式配置优先，否则取第一个可用的。"""
        if self._emb_resolved:
            return self._emb_provider
        self._emb_resolved = True
        provider = None
        pid = str(self._get("embedding_provider_id", "") or "")
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider is None:
                self._warn_missing_provider("embedding", pid, embed=True)
        if provider is None:
            try:
                providers = self.context.get_all_embedding_providers()
            except Exception:
                providers = []
            provider = providers[0] if providers else None
        self._emb_provider = provider
        if provider is not None:
            cfg_id = ""
            try:
                cfg_id = str(provider.provider_config.get("id", "") or "")
            except Exception:
                cfg_id = ""
            model = ""
            try:
                model = str(provider.get_model() or "")
            except Exception:
                model = ""
            self._emb_model = f"{cfg_id or pid}:{model}"
            logger.info(f"[memory_evo] 语义召回使用 embedding: {self._emb_model}")
        else:
            logger.info("[memory_evo] 未配置 embedding provider，召回退回字面匹配")
        return provider

    async def _embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        provider = self._embedder()
        if provider is None or not texts:
            return None
        try:
            timeout = float(self._get("embedding_timeout", 10)) + 5
            return await asyncio.wait_for(provider.get_embeddings(texts), timeout=timeout)
        except Exception:
            logger.debug("[memory_evo] 批量 embedding 失败，尝试逐条", exc_info=True)
        out: list[list[float]] = []
        for text in texts:
            try:
                out.append(await asyncio.wait_for(
                    provider.get_embedding(text), timeout=float(self._get("embedding_timeout", 10))
                ))
            except Exception:
                logger.warning("[memory_evo] embedding 失败，跳过该条")
                return None
        return out

    async def _embed_new(self, scope: str, items: list[tuple[int, str]]) -> None:
        if not items or not self._get("semantic_recall", True):
            return
        vectors = await self._embed_texts([t for _, t in items])
        if not vectors or len(vectors) != len(items):
            return
        for (fact_id, _), vec in zip(items, vectors):
            try:
                await asyncio.to_thread(self.store.save_embedding, scope, fact_id, vec, self._emb_model)
            except Exception:
                logger.debug("[memory_evo] 写入向量失败 fact#%s", fact_id, exc_info=True)

    async def _backfill_embeddings(self, scope: str, limit: int = 32) -> int:
        if not self._get("semantic_recall", True):
            return 0
        if self._embedder() is None:
            return 0
        missing = await asyncio.to_thread(self.store.missing_embeddings, scope, self._emb_model, limit)
        if not missing:
            return 0
        await self._embed_new(scope, missing)
        return len(missing)

    async def _ask(
        self, umo: str, system: str, prompt: str, provider_id: str | None = None
    ) -> dict:
        provider = await self._provider(umo, provider_id)
        if not provider:
            logger.debug("[memory_evo] 没有可用 provider，跳过本次抽取")
            return {}
        timeout = float(self._get("llm_timeout", 45))
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, system_prompt=system),
            timeout=timeout,
        )
        return extractor.parse_json(getattr(resp, "completion_text", "") or "")

    # ---------------- 读：每轮注入 ----------------

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._get("enable", True):
            return
        self._start_janitor()
        scope = event.unified_msg_origin
        text = event.message_str or ""
        self._scopes.add(scope)
        if text:
            try:
                await asyncio.to_thread(self.store.touch_activity, scope)
            except Exception:
                logger.debug("[memory_evo] 记录活跃时间失败", exc_info=True)
        blocks: list[str] = []

        query_vec = None
        if text and self._get("semantic_recall", True) and self._embedder() is not None:
            try:
                query_vec = await asyncio.wait_for(
                    self._emb_provider.get_embedding(text),
                    timeout=float(self._get("embedding_timeout", 10)),
                )
            except Exception:
                logger.debug("[memory_evo] query embedding 失败，本轮退回字面匹配", exc_info=True)
                query_vec = None

        if text:
            self._remember_turn(scope, "user", text)
        try:
            facts = await asyncio.to_thread(
                self.store.recall,
                scope,
                text,
                int(self._get("top_k", 6)),
                int(self._get("max_inject_chars", 600)),
                float(self._get("min_confidence", 0.45)),
                bool(self._get("include_suspect", False)),
                int(self._get("max_per_predicate", 2)),
                int(self._get("max_item_chars", 80)),
                query_vec,
                self._emb_model if query_vec else None,
            )
            if facts:
                lines = [f"- {f.subject}|{f.predicate}：{f.object}" for f in facts]
                blocks.append("<memory>\n" + "\n".join(lines) + "\n</memory>")
        except Exception:
            logger.exception("[memory_evo] 记忆召回失败")

        if self._get("persona_inject", True):
            try:
                state = await asyncio.to_thread(self.store.get_persona, scope, self.baseline)
                rendered = persona.render(
                    state["traits"],
                    state.get("mood", ""),
                    int(self._get("persona_max_chars", 160)),
                )
                blocks.append("<self_state>\n" + rendered + "\n</self_state>")
            except Exception:
                logger.exception("[memory_evo] 人格状态读取失败")

        if blocks:
            req.extra_user_content_parts.append(TextPart(text="\n".join(blocks)))

    # ---------------- 写：缓冲 + 异步抽取 ----------------

    def _remember_turn(self, scope: str, role: str, text: str) -> None:
        buf = self.buffers.setdefault(scope, deque(maxlen=int(self._get("max_buffer", 40))))
        now = time.time()
        # 工具循环会让同一条用户消息多次触发 on_llm_request，去重避免污染抽取
        if buf and buf[-1][0] == role and buf[-1][1] == text and now - buf[-1][2] < 10:
            return
        buf.append((role, text, now))
        turns = sum(1 for role_, _, _ in buf if role_ == "user")
        if turns >= int(self._get("flush_turns", 6)):
            asyncio.create_task(self._flush(scope, reason="turns"))

    @filter.on_llm_response()
    async def record_reply(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        if not self._get("enable", True):
            return
        text = getattr(resp, "completion_text", "") or ""
        if text:
            self._remember_turn(event.unified_msg_origin, "assistant", text)

    async def _flush(self, scope: str, reason: str = "manual") -> None:
        if scope in self.inflight:
            return
        buf = self.buffers.get(scope)
        if not buf:
            return
        self.inflight.add(scope)
        try:
            items = [(r, t) for r, t, _ in buf]
            buf.clear()
            transcript = extractor.build_transcript(items)
            if not transcript.strip():
                return

            data = await self._ask(
                scope,
                extractor.FACT_SYSTEM,
                extractor.FACT_PROMPT.replace("__TRANSCRIPT__", transcript),
            )
            pending_embeddings: list[tuple[int, str]] = []
            for fact in extractor.sanitize_facts(data):
                fact_id, action = await asyncio.to_thread(
                    self.store.upsert_fact,
                    scope,
                    fact["subject"],
                    fact["predicate"],
                    fact["object"],
                    fact["kind"],
                    fact["importance"],
                    fact["confidence"],
                    fact["evidence"],
                )
                pending_embeddings.append(
                    (fact_id, f"{fact['subject']} {fact['predicate']} {fact['object']}")
                )
                logger.debug(f"[memory_evo] fact#{fact_id} {action} ({reason}) scope={scope}")
            await self._embed_new(scope, pending_embeddings)

            if self._get("persona_evolve", True):
                pens = await self._ask(
                    scope,
                    extractor.PERSONA_SYSTEM,
                    extractor.PERSONA_PROMPT.replace("__TRANSCRIPT__", transcript),
                )
                deltas, mood, why = extractor.sanitize_persona(pens)
                state = await asyncio.to_thread(self.store.get_persona, scope, self.baseline)
                traits = persona.apply_deltas(
                    state["traits"],
                    deltas,
                    max_step=float(self._get("max_step", 0.06)),
                    decay=float(self._get("decay", 0.01)),
                    baseline=self.baseline,
                )
                await asyncio.to_thread(
                    self.store.save_persona, scope, traits, mood or state.get("mood", ""), why
                )
        except asyncio.TimeoutError:
            logger.warning(f"[memory_evo] 抽取超时，本轮跳过 scope={scope}")
        except Exception:
            logger.exception(f"[memory_evo] flush 失败 scope={scope} reason={reason}")
        finally:
            self.inflight.discard(scope)

    # ---------------- 主动开口 ----------------

    def _care_cfg(self) -> dict:
        return {
            "enabled": bool(self._get("care_enable", False)),
            "min_idle_minutes": float(self._get("care_min_idle_minutes", 90)),
            "daily_max": int(self._get("care_daily_max", 3)),
            "max_unanswered": int(self._get("care_max_unanswered", 2)),
            "quiet_start": str(self._get("care_quiet_start", "23:30")),
            "quiet_end": str(self._get("care_quiet_end", "08:00")),
            "probability": float(self._get("care_probability", 0.85)),
            "max_chars": int(self._get("care_max_chars", 45)),
            "schedule": str(self._get("care_schedule", "") or ""),
        }

    @staticmethod
    def _is_friend_scope(scope: str) -> bool:
        parts = scope.split(":")
        return len(parts) >= 2 and parts[1] == MessageType.FRIEND_MESSAGE.value

    async def _care_tick(self) -> None:
        if not self._get("care_enable", False):
            return
        for scope in list(self._scopes):
            if not self._is_friend_scope(scope):
                continue
            try:
                await self._maybe_care(scope)
            except Exception:
                logger.exception(f"[memory_evo] 主动消息检查失败 scope={scope}")

    async def _maybe_care(self, scope: str, force: bool = False) -> tuple[bool, str]:
        """检查并（可能）主动发一条。返回 (是否已发送, 原因)。"""
        if not self._is_friend_scope(scope):
            return False, "not_private"
        state = await asyncio.to_thread(self.store.get_care_state, scope)
        tz_name = str(self._get("care_timezone", proactive.DEFAULT_TZ))
        now = proactive.local_now(tz_name)
        ok, reason = proactive.decide(now, state, self._care_cfg(), force=force)
        if not ok:
            logger.debug(f"[memory_evo] 主动消息跳过：{reason} scope={scope}")
            return False, reason

        facts = await asyncio.to_thread(
            self.store.recall, scope, "", 4, 400, 0.45, False, 2, 60
        )
        p_state = await asyncio.to_thread(self.store.get_persona, scope, self.baseline)
        persona_text = persona.render(p_state["traits"], p_state.get("mood", ""), 80)
        memory_lines = [f"{f.predicate}：{f.object}" for f in facts]
        max_chars = int(self._get("care_max_chars", 45))
        prompt = proactive.render_prompt(now, state, persona_text, memory_lines, max_chars)
        data = await self._ask(
            scope,
            proactive.CARE_SYSTEM,
            prompt,
            provider_id=str(self._get("care_provider_id", "") or ""),
        )
        text, gap, why = proactive.sanitize_reply(data, max_chars)
        if not text:
            await asyncio.to_thread(self.store.mark_care_skipped, scope, gap)
            logger.debug(f"[memory_evo] 主动消息选择沉默：{why} gap={gap}")
            return False, why

        sent = await self.context.send_message(scope, MessageChain([Plain(text)]))
        if not sent:
            logger.warning(f"[memory_evo] 主动消息发送失败（找不到平台） scope={scope}")
            return False, "send_failed"
        await asyncio.to_thread(
            self.store.mark_care_sent,
            scope,
            now.timestamp(),
            now.strftime("%Y-%m-%d"),
            text,
            gap,
        )
        logger.info(f"[memory_evo] 已主动发送 scope={scope} gap={gap}min text={text[:30]!r}")
        return True, "sent"

    # ---------------- 模型可用工具 ----------------

    @filter.llm_tool(name="memory_search")
    async def tool_search(self, event: AstrMessageEvent, query: str, k: int = 6) -> str:
        """搜索关于用户的长期记忆。

        Args:
            query(string): 搜索关键词，比如"饮食偏好"
            k(number): 返回条数，默认 6
        """
        k = max(1, min(20, int(k)))
        facts = await asyncio.to_thread(
            self.store.recall,
            event.unified_msg_origin,
            query,
            k,
            1200,
            float(self._get("min_confidence", 0.45)),
            True,
            3,
            200,
        )
        if not facts:
            return "没有找到相关记忆。"
        lines = []
        for f in facts:
            flag = "（待确认，用户确认后才会自动使用）" if f.status == "suspect" else ""
            lines.append(
                f"#{f.id} {f.subject}|{f.predicate}：{f.object}"
                f"（重要度 {f.importance}，置信度 {f.confidence:.2f}）{flag}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="memory_write")
    async def tool_write(self, event: AstrMessageEvent, content: str, kind: str = "fact", importance: int = 3) -> str:
        """记录一条长期记忆。用户明确要求记住，或出现值得长期保留的信息时调用。

        Args:
            content(string): 一句话事实，例如"用户对花生过敏"
            kind(string): preference/profile/event/task/fact 之一
            importance(number): 1-5，5 最重要
        """
        fact_id, action = await asyncio.to_thread(
            self.store.upsert_fact,
            event.unified_msg_origin,
            "用户",
            content[:40],
            content,
            kind if kind in {"preference", "profile", "event", "task", "fact"} else "fact",
            max(1, min(5, int(importance))),
            0.95,
            "模型主动写入",
        )
        if action == "conflict":
            return f"已记录 #{fact_id}，但与既有记忆矛盾，已标记待确认；请向用户确认以哪条为准。"
        return f"已记录 #{fact_id}（{action}）"

    @filter.llm_tool(name="memory_forget")
    async def tool_forget(self, event: AstrMessageEvent, fact_id: int) -> str:
        """删除一条记忆。

        Args:
            fact_id(number): 记忆编号，来自 memory_search
        """
        ok = await asyncio.to_thread(self.store.delete_fact, event.unified_msg_origin, int(fact_id))
        return "已删除" if ok else "没有这条记忆"

    # ---------------- 管理命令 ----------------

    @filter.command("memory")
    async def memory_cmd(self, event: AstrMessageEvent):
        """记忆管理：/memory [dump|why <词>|confirm <id>|forget <id|词|all>|persona]"""
        scope = event.unified_msg_origin
        parts = (event.message_str or "").split()
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "dump":
            rows = await asyncio.to_thread(self.store.list_facts, scope, 50)
            if not rows:
                yield event.plain_result("这个会话还没有记忆。")
                return
            lines = []
            for r in rows:
                flag = "⚠️待确认 " if r["status"] == "suspect" else ""
                lines.append(
                    f"{flag}#{r['id']} [{r['kind']}] {r['subject']}|{r['predicate']}：{r['object']}"
                )
            yield event.plain_result(f"共 {len(rows)} 条：\n" + "\n".join(lines))
            return

        if sub == "why":
            query = " ".join(parts[2:])
            facts = await asyncio.to_thread(self.store.recall, scope, query, 10, 2000)
            if not facts:
                yield event.plain_result("没有匹配的记忆。")
                return
            body = "\n".join(f"{f.score:.3f}  #{f.id} {f.predicate}：{f.object}" for f in facts)
            yield event.plain_result(f"召回打分：\n{body}")
            return

        if sub == "confirm":
            arg = " ".join(parts[2:]).strip()
            if not arg.isdigit():
                yield event.plain_result("用法：/memory confirm <id>（id 见 /memory dump 里的 ⚠️）")
                return
            ok = await asyncio.to_thread(self.store.confirm_fact, scope, int(arg))
            yield event.plain_result("已确认，冲突的另一条已归档。" if ok else "没有这条记忆。")
            return

        if sub == "forget":
            arg = " ".join(parts[2:]).strip()
            if not arg:
                yield event.plain_result("用法：/memory forget <id|关键词|all>")
                return
            if arg.lower() == "all":
                count = await asyncio.to_thread(self.store.clear_scope, scope)
            elif arg.isdigit():
                ok = await asyncio.to_thread(self.store.delete_fact, scope, int(arg))
                count = 1 if ok else 0
            else:
                count = await asyncio.to_thread(self.store.forget_matching, scope, arg)
            yield event.plain_result(f"已删除 {count} 条。")
            return

        if sub == "care":
            arg = parts[2].lower() if len(parts) > 2 else "status"
            if arg in ("on", "off"):
                if not self._get("care_enable", False):
                    yield event.plain_result(
                        "主动消息总开关（care_enable）还没开，先去插件设置里打开，再单独开关这个会话。"
                    )
                    return
                await asyncio.to_thread(self.store.set_care_enabled, scope, arg == "on")
                yield event.plain_result(
                    "这个会话的主动消息已开启。" if arg == "on" else "已关闭这个会话的主动消息。"
                )
                return
            if arg == "now":
                sent, reason = await self._maybe_care(scope, force=True)
                yield event.plain_result("已发出一条。" if sent else f"没发：{reason}")
                return
            state = await asyncio.to_thread(self.store.get_care_state, scope)
            cfg = self._care_cfg()
            idle = (time.time() - float(state["last_active"])) / 60 if state["last_active"] else -1
            body = [
                f"总开关：{'开' if cfg['enabled'] else '关'}",
                f"本会话：{'开' if state.get('enabled', 1) else '关'}",
                (f"空闲：{idle:.0f} 分钟" if idle >= 0 else "还没记录到活跃时间"),
                f"今日已发：{state['sent_today']} / {cfg['daily_max']}",
                f"未回应：{state['unanswered']} / {cfg['max_unanswered']}",
                f"下次最早：{state['next_gap_minutes']} 分钟后",
                f"免打扰：{cfg['quiet_start']} - {cfg['quiet_end']}",
                ("允许时段：" + (cfg['schedule'] or "不限")),
            ]
            yield event.plain_result(chr(10).join(body))
            return

        if sub == "reindex":
            added = await self._backfill_embeddings(scope, limit=64)
            done, total = await asyncio.to_thread(
                self.store.embedding_stats, scope, self._emb_model
            )
            yield event.plain_result(f"本轮补写 {added} 条向量，已覆盖 {done}/{total}。")
            return

        if sub == "persona":
            state = await asyncio.to_thread(self.store.get_persona, scope, self.baseline)
            timeline = await asyncio.to_thread(self.store.persona_timeline, scope, 5)
            traits = "，".join(f"{persona.TRAIT_LABELS[k]}={state['traits'][k]:.2f}" for k in persona.TRAIT_KEYS)
            body = [f"心情：{state.get('mood') or '（无）'}", traits]
            for row in timeline:
                body.append(f"v{row['id']} {row['reason'][:40]}")
            yield event.plain_result("\n".join(body))
            return

        yield event.plain_result(HELP_TEXT)

    async def terminate(self) -> None:
        if self._janitor and not self._janitor.done():
            self._janitor.cancel()
        for scope in list(self.buffers):
            try:
                await self._flush(scope, reason="shutdown")
            except Exception:
                pass
        self.store.close()
