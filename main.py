"""memory_evo：小规模长期记忆 + 人格演化。"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import extractor, persona
from .store import MemoryStore

PLUGIN_NAME = "astrbot_plugin_memory_evo"

HELP_TEXT = (
    "/memory dump 查看记忆（⚠️ 表示待确认）\n"
    "/memory why <词> 看召回打分\n"
    "/memory confirm <id> 确认一条待确认记忆\n"
    "/memory reindex 补写缺失语义向量\n"
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
        self._emb_resolved = False
        self._emb_provider = None
        self._emb_model = ""
        self._janitor: asyncio.Task | None = None
        self._start_janitor()

    # ---------------- 基础设施 ----------------

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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[memory_evo] janitor 异常退出")

    async def _provider(self, umo: str):
        provider_id = str(self._get("extract_provider_id", "") or "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
        return await self.context.get_using_provider_async(umo)

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

    async def _ask(self, umo: str, system: str, prompt: str) -> dict:
        provider = await self._provider(umo)
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
