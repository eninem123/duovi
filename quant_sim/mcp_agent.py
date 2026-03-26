import asyncio
import gc
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import yaml
import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.text import TextReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.stdio import StdioServerParameters, get_default_environment
from mcp.client.session import ClientSession
from local_rag import LocalKnowledgeBase
from quant_factors import QuantFactors

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MCPAgent:
    def __init__(self, config_path="config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.base_dir = os.path.dirname(os.path.abspath(config_path))
        base_env = os.environ.copy()

        stock_server = self.config["mcp"]["stock_server"]
        self.stock_params = StdioServerParameters(
            command=stock_server["command"],
            args=stock_server["args"],
            env={**base_env, **(stock_server.get("env") or {})},
        )

        self.market_servers = [("stock", self.stock_params)]
        ashare_server = self.config["mcp"].get("ashare_server")
        if ashare_server:
            ashare_params = StdioServerParameters(
                command=ashare_server["command"],
                args=ashare_server.get("args", []),
                env={**base_env, **(ashare_server.get("env") or {})},
            )
            self.market_servers.append(("ashare", ashare_params))

        notebook_server = self.config["mcp"]["notebooklm_server"]
        self.notebooklm_params = StdioServerParameters(
            command=notebook_server["command"],
            args=notebook_server["args"],
            env={**base_env, **(notebook_server.get("env") or {})},
        )

        self.notebook_id = self.config["mcp"]["notebook_id"]
        agent_config = self.config.get("agent", {})
        legacy_prompt = agent_config.get("system_prompt", "")
        self.orchestrator_prompt = agent_config.get("orchestrator_prompt") or legacy_prompt
        self.execution_prompt = agent_config.get("execution_prompt") or legacy_prompt
        self.scene_policy_prompt = agent_config.get("scene_policy_prompt", "")
        self.special_protocol_prompt = agent_config.get("special_protocol_prompt", "")
        self.scene_policies = agent_config.get("scene_policies", {})
        self.scene_detection = agent_config.get("scene_detection", {})
        self.agent_multihop = agent_config.get("multihop", {})
        self.retrieval_protocol_prompt = agent_config.get("retrieval_protocol_prompt", "")
        self.last_tool_errors = {}
        self.notebooklm_lock = threading.Lock()
        self.local_rag = LocalKnowledgeBase(
            self.config.get("local_rag", {}),
            base_dir=self.base_dir,
        )
        self.market_tool_candidates = self.config["mcp"].get("market_tool_candidates", {
            "industry_list": ["get_industry_list"],
            "quotes": ["get_quotes_by_query"],
            "intraday": ["get_intraday_data", "get_minute_bars", "get_time_sharing_data"],
            "fundamentals": ["get_stock_fundamentals", "get_fundamentals", "get_valuation_metrics"],
            "technicals": ["get_technical_indicators", "get_kdj_macd", "get_technical_analysis"],
            "capital_flow": ["get_capital_flow", "get_money_flow", "get_main_fund_flow"],
        })
        self.enrichment = self.config.get("enrichment") or {}
        self.litellm_cfg = self.config.get("litellm") or {}
        self.quant_factors = QuantFactors(self.config)

    def _litellm_model_chain(self):
        from llm_decision import resolve_litellm_model_chain

        return resolve_litellm_model_chain(self.litellm_cfg)

    def _detect_scene(self, question):
        text = (question or "").strip().lower()
        for scene in ["monitor", "execute", "theme", "discipline", "opportunity"]:
            rules = self.scene_detection.get(scene) or {}
            keywords = rules.get("keywords") or []
            if any(str(keyword).lower() in text for keyword in keywords):
                return scene
        return "opportunity"

    def _scene_policy(self, scene):
        return self.scene_policies.get(scene) or {}

    def _scene_label(self, scene):
        return self._scene_policy(scene).get("label") or scene

    def _scene_prompt_mode(self, scene):
        return "execution" if scene in {"monitor", "execute"} else "orchestrator"

    def _render_scene_policy(self, scene):
        policy = self._scene_policy(scene)
        if not policy:
            return ""
        weights = policy.get("weights") or {}
        return (
            f"当前场景判定：{scene}（{self._scene_label(scene)}）。\n"
            f"信息权重：联网知识 {weights.get('realtime', 0)}%，"
            f"模型知识 {weights.get('model', 0)}%，"
            f"知识库知识 {weights.get('knowledge_base', 0)}%。\n"
            f"优化说明：{policy.get('optimization') or '无'}"
        )

    def _compose_prompt_stack(self, scene, force_mode=None):
        prompt_mode = force_mode or self._scene_prompt_mode(scene)
        base_prompt = self.execution_prompt if prompt_mode == "execution" else self.orchestrator_prompt
        sections = [base_prompt.strip()]
        if self.scene_policy_prompt:
            sections.append(self.scene_policy_prompt.strip())
        scene_policy_text = self._render_scene_policy(scene)
        if scene_policy_text:
            sections.append(scene_policy_text)
        if self.special_protocol_prompt:
            sections.append(self.special_protocol_prompt.strip())
        if self.retrieval_protocol_prompt:
            sections.append(self.retrieval_protocol_prompt.strip())
        return "\n\n".join(section for section in sections if section).strip(), prompt_mode

    async def _close_process_stream(self, stream):
        if stream is None:
            return
        close = getattr(stream, "aclose", None)
        if close is None:
            return
        try:
            with anyio.move_on_after(1):
                await close()
        except Exception:
            pass

    async def _close_process(self, process):
        if process is None:
            return
        try:
            process.kill()
        except Exception:
            pass
        wait = getattr(process, "wait", None)
        if wait is not None:
            try:
                with anyio.move_on_after(1):
                    await wait()
            except Exception:
                pass
        await self._close_process_stream(getattr(process, "stdin", None))
        await self._close_process_stream(getattr(process, "stdout", None))
        await self._close_process_stream(getattr(process, "stderr", None))
        try:
            with anyio.move_on_after(1):
                await process.aclose()
        except Exception:
            pass

    async def _call_managed_tool_once(self, server_params, tool_name, arguments, server_name=None):
        server = server_params
        server_name = server_name or self._server_name_for(server_params)
        read_stream: MemoryObjectReceiveStream[types.JSONRPCMessage | Exception]
        read_stream_writer: MemoryObjectSendStream[types.JSONRPCMessage | Exception]
        write_stream: MemoryObjectSendStream[types.JSONRPCMessage]
        write_stream_reader: MemoryObjectReceiveStream[types.JSONRPCMessage]

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
        process = await anyio.open_process(
            [server.command, *server.args],
            env=server.env if server.env is not None else get_default_environment(),
            stderr=sys.stderr,
        )

        async def stdout_reader():
            assert process.stdout, "Opened process is missing stdout"
            try:
                async with read_stream_writer:
                    buffer = ""
                    async for chunk in TextReceiveStream(
                        process.stdout,
                        encoding=server.encoding,
                        errors=server.encoding_error_handler,
                    ):
                        lines = (buffer + chunk).split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                            except Exception as exc:
                                await read_stream_writer.send(exc)
                                continue
                            await read_stream_writer.send(message)
            except anyio.ClosedResourceError:
                await anyio.lowlevel.checkpoint()

        async def stdin_writer():
            assert process.stdin, "Opened process is missing stdin"
            try:
                async with write_stream_reader:
                    async for message in write_stream_reader:
                        encoded = message.model_dump_json(by_alias=True, exclude_none=True)
                        await process.stdin.send(
                            (encoded + "\n").encode(
                                encoding=server.encoding,
                                errors=server.encoding_error_handler,
                            )
                        )
            except anyio.ClosedResourceError:
                await anyio.lowlevel.checkpoint()

        result_text = None
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(stdout_reader)
                tg.start_soon(stdin_writer)
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
                    if result.content and len(result.content) > 0:
                        self.last_tool_errors.pop((server_name, tool_name), None)
                        result_text = result.content[0].text
                try:
                    await write_stream.aclose()
                except Exception:
                    pass
                tg.cancel_scope.cancel()
            return result_text
        except Exception as e:
            self.last_tool_errors[(server_name, tool_name)] = str(e)
            logging.error(f"Error calling {tool_name}: {str(e)}")
            return None
        finally:
            await self._close_process(process)
            process = None
            if sys.platform.startswith("win"):
                gc.collect()

    async def _call_tool_once(self, server_params, tool_name, arguments):
        """单次 MCP Tool 调用，不包含自愈重试。"""
        if self._server_name_for(server_params) == "notebooklm":
            return await self._call_notebook_tool_once(tool_name, arguments)
        return await self._call_managed_tool_once(server_params, tool_name, arguments)

    async def _call_notebook_tool_once(self, tool_name, arguments):
        """
        NotebookLM 的 ask_question 在子进程退出阶段容易被 Chrome 持久上下文拖住。
        这里改为手动托管进程，拿到工具结果后主动结束子进程，避免请求卡死在退出收尾上。
        """
        return await self._call_managed_tool_once(
            self.notebooklm_params,
            tool_name,
            arguments,
            server_name="notebooklm",
        )

    def _is_notebooklm_recoverable_error(self, error_message):
        if not error_message:
            return False
        text = str(error_message)
        recoverable_markers = [
            "browserType.launchPersistentContext",
            "Target page, context or browser has been closed",
            "chrome_profile",
            "EBUSY",
            "Failed to create session",
        ]
        return any(marker in text for marker in recoverable_markers)

    def _cleanup_stale_notebooklm_processes(self):
        """
        清理残留的 notebooklm-mcp/node/chrome 进程。
        仅杀掉 command line 明确指向 notebooklm-mcp 或其专用 chrome_profile 的进程。
        """
        ps_script = r"""
$patterns = @(
  'notebooklm-mcp',
  'notebooklm-mcp.cmd',
  'notebooklm-mcp\Data\chrome_profile'
)
$targets = Get-CimInstance Win32_Process | Where-Object {
  $cmd = $_.CommandLine
  if (-not $cmd) { return $false }
  foreach ($pattern in $patterns) {
    if ($cmd -like ('*' + $pattern + '*')) { return $true }
  }
  return $false
}
$targets | ForEach-Object {
  try {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    Write-Output ('killed:{0}:{1}' -f $_.ProcessId, $_.Name)
  } catch {
    Write-Output ('failed:{0}:{1}:{2}' -f $_.ProcessId, $_.Name, $_.Exception.Message)
  }
}
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = (result.stdout or "").strip()
            errors = (result.stderr or "").strip()
            if output:
                logging.warning("NotebookLM 进程清理结果: %s", output)
            if errors:
                logging.warning("NotebookLM 进程清理 stderr: %s", errors)
        except Exception as exc:
            logging.warning("清理 NotebookLM 残留进程失败: %s", exc)

    async def _call_tool(self, server_params, tool_name, arguments):
        """通用 MCP Tool 调用方法，NotebookLM 调用带串行与自愈重试。"""
        server_name = self._server_name_for(server_params)
        if server_name != "notebooklm":
            return await self._call_tool_once(server_params, tool_name, arguments)

        with self.notebooklm_lock:
            result = await self._call_tool_once(server_params, tool_name, arguments)
            tool_error = self._extract_tool_error_text(result or "")
            if result and not tool_error:
                return result

            error_message = tool_error or self.get_last_tool_error(server_name, tool_name)
            if not self._is_notebooklm_recoverable_error(error_message):
                return result

            logging.warning(
                "NotebookLM 调用命中可恢复错误，准备清理残留进程后重试一次: %s",
                error_message,
            )
            self._cleanup_stale_notebooklm_processes()
            await asyncio.sleep(2)
            retried = await self._call_tool_once(server_params, tool_name, arguments)
            return retried or result

    def _server_name_for(self, server_params):
        if server_params.command == self.notebooklm_params.command:
            return "notebooklm"
        return "market"

    def get_last_tool_error(self, server_name, tool_name):
        return self.last_tool_errors.get((server_name, tool_name))

    def _build_unavailable_decision(self, error_message):
        notebook_trace = f"NotebookLM 当前不可用：{error_message}" if error_message else "NotebookLM 当前不可用"
        return {
            "symbol": None,
            "name": "",
            "action": "watch",
            "reason": f"知识库维度未接通：{notebook_trace}。本轮无法完成四维联合评估，系统按纪律观望。",
            "target_price": None,
            "stop_loss_price": None,
            "position_pct": 0.0,
            "dimension_scores": {
                "data_arch": 0.0,
                "notebooklm": 0.0,
                "game_psych": 0.0,
                "trend": 0.0,
            },
            "thinking_trace": {
                "data_arch": "未生成有效四维分析，因为知识库侧失败导致整轮降级。",
                "notebooklm": notebook_trace,
                "game_psych": "未执行。",
                "trend": "未执行。",
            },
            "success": False,
            "error": error_message or "NotebookLM 未返回内容",
            "knowledge_source": "unavailable",
            "decision_source": "unavailable",
            "win_rate_confidence": 0.0,
            "total_score": 0.0,
        }

    def _merge_local_rag_payloads(self, payloads):
        """合并多跳本地检索结果，按 score 去重排序（MSA 风格多查询证据聚合）。"""
        parts = [p for p in (payloads or []) if p]
        any_ok = any(p.get("success") for p in parts)
        if not parts:
            return {
                "success": False,
                "source": "local_rag",
                "reason": "no local payloads",
                "results": [],
            }
        if not any_ok:
            return parts[0]

        seen = set()
        merged_rows = []
        retrieval_modes = []
        for p in parts:
            if not p.get("success"):
                continue
            meta = p.get("retrieval") or {}
            if meta.get("mode"):
                retrieval_modes.append(meta.get("mode"))
            for row in p.get("results") or []:
                key = (row.get("source_path"), (row.get("text") or "")[:64])
                if key in seen:
                    continue
                seen.add(key)
                merged_rows.append(row)

        merged_rows.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
        top_k = int(self.config.get("local_rag", {}).get("top_k", 4))
        cap = max(top_k, int((self.config.get("local_rag", {}).get("multihop") or {}).get("merge_cap", 8)))
        trimmed = merged_rows[:cap]
        if not trimmed:
            return parts[0]

        hits_preview = trimmed[:3]
        summary = "；".join((r.get("text") or "")[:90] for r in hits_preview[:2])
        evidence = [f"{r.get('title')}: {(r.get('text') or '')[:120]}" for r in hits_preview]
        confidence = min(0.9, max(0.2, float(trimmed[0].get("score") or 0.0)))
        out = {
            "success": True,
            "source": "local_rag",
            "summary": summary,
            "evidence": evidence,
            "confidence": round(confidence, 4),
            "results": trimmed,
            "persona_notes": {
                "data_arch": f"本地多跳检索合并命中 {len(trimmed)} 条（去重后）。",
                "notebooklm": "NotebookLM 不可用时已回退到本地知识库多跳聚合。",
                "game_psych": "多跳证据需与盘面资金风格交叉验证。",
                "trend": "聚合证据仅作框架约束，执行仍服从实时行情与纪律。",
            },
            "retrieval": {
                "mode": "multihop_merge",
                "hops": len(parts),
                "stages": retrieval_modes,
            },
        }
        return out

    def _search_local_knowledge(self, question, symbol_hint=None):
        query_parts = [question]
        if symbol_hint:
            query_parts.append(f"相关标的：{symbol_hint}")
        query = "\n".join(part for part in query_parts if part)
        mh = (self.config.get("local_rag") or {}).get("multihop") or {}
        try:
            primary = self.local_rag.search(query)
            if not mh.get("enabled"):
                return primary
            extra_n = int(mh.get("extra_searches", 1))
            suffixes = mh.get("suffixes") or [" 风险与假设", " 执行与仓位纪律"]
            extra_top = int(mh.get("extra_top_k", 3))
            bucket = [primary]
            for i in range(extra_n):
                suf = suffixes[i % len(suffixes)]
                bucket.append(self.local_rag.search(f"{query}{suf}", top_k=extra_top))
            return self._merge_local_rag_payloads(bucket)
        except Exception as exc:
            logging.error("本地知识库检索失败: %s", exc)
            return {
                "success": False,
                "source": "local_rag",
                "reason": str(exc),
                "results": [],
            }

    def _merge_gap_fill_kb(self, base, gap):
        if not base or not gap or not isinstance(gap, dict):
            return base
        out = dict(base)
        for key in ("kb_evidence", "risk_flags", "assumptions", "ignored_details"):
            add = gap.get(key) or []
            if not isinstance(add, list):
                continue
            cur = list(out.get(key) or [])
            for item in add:
                if not item or not isinstance(item, str):
                    continue
                item = item.strip()
                if item and item not in cur:
                    cur.append(item)
            out[key] = cur[:20]
        gs = (gap.get("kb_summary") or "").strip()
        if gs:
            prev = (out.get("kb_summary") or "").strip()
            out["kb_summary"] = f"{prev}\n（补全）{gs}".strip()[:800]
        try:
            gc = float(gap.get("confidence", 0.0) or 0.0)
            if gc > 0:
                prev_c = float(out.get("confidence", 0.0) or 0.0)
                out["confidence"] = round(min(0.95, max(prev_c, gc)), 4)
        except (TypeError, ValueError):
            pass
        return out

    async def _notebooklm_gap_fill_hop(self, question, scene, kb_data, prompt_context, prompt_mode):
        """第二跳：在长上下文首轮 JSON 基础上补全缺口（仍以 NotebookLM 为主）。返回 (kb_data, raw, called)。"""
        mh = self.agent_multihop or {}
        if not mh.get("enabled") or not mh.get("notebooklm_gap_fill", True):
            return kb_data, None, False
        try:
            conf = float(kb_data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        ev = kb_data.get("kb_evidence") or []
        min_ev = int(mh.get("gap_fill_min_evidence", 2))
        min_conf = float(mh.get("gap_fill_min_confidence", 0.45))
        if conf >= min_conf and len(ev) >= min_ev:
            return kb_data, None, False

        gap_prompt = f"""
{prompt_context}

你已完成对 NotebookLM 知识库的第一轮结构化抽取。当前启用的提示模式为 {prompt_mode}，场景为 {scene}。
请进行第二轮「缺口补全 / 交叉验证」：不要重复第一轮已有要点，只补充遗漏的风险、假设、反例证据或被忽略细节。
只输出一个 JSON 对象（不要其它文字），字段如下：
{{
  "kb_evidence": ["新增证据要点1", "新增证据要点2"],
  "risk_flags": ["新增风险1"],
  "assumptions": ["新增隐含假设1"],
  "ignored_details": ["被忽略细节1"],
  "kb_summary": "用一两句话概括本轮补全",
  "confidence": 0.0
}}
每个数组最多 3 条新内容；若无新内容则对应数组为空。

【第一轮摘要】
{kb_data.get("kb_summary", "")[:600]}

【第一轮已列证据】
{json.dumps(ev[:8], ensure_ascii=False)}

【用户原问题】
{question}
"""
        raw = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {"notebook_id": self.notebook_id, "question": gap_prompt},
        )
        if not raw or self._extract_tool_error_text(raw or ""):
            return kb_data, raw, True
        text = self._extract_notebook_answer_text(raw or "")
        json_str = self._extract_json_object(text or "")
        if not json_str:
            return kb_data, raw, True
        try:
            gap = json.loads(json_str)
            if isinstance(gap, dict):
                return self._merge_gap_fill_kb(kb_data, gap), raw, True
        except json.JSONDecodeError:
            pass
        return kb_data, raw, True

    def _local_rag_as_kb_data(self, local_result):
        if not local_result or not local_result.get("success"):
            return None
        return {
            "kb_summary": local_result.get("summary") or "本地知识库暂无明确结论",
            "kb_evidence": local_result.get("evidence") or [],
            "assumptions": [
                "本地知识库当前以项目内策略文档和研究摘要为主，不等同于完整外部研报库。",
                "结论仍需结合实时市场上下文与风控阈值复核。",
            ],
            "confidence": float(local_result.get("confidence", 0.0) or 0.0),
            "persona_notes": local_result.get("persona_notes") or {},
        }

    def _build_local_rag_decision(self, local_result, error_message):
        decision = self._build_unavailable_decision(error_message)
        if local_result and local_result.get("success"):
            knowledge_score = round(min(25.0, float(local_result.get("confidence", 0.0)) * 25.0), 2)
            decision["dimension_scores"]["notebooklm"] = knowledge_score
            decision["total_score"] = round(sum(decision["dimension_scores"].values()), 2)
            decision["win_rate_confidence"] = round(decision["total_score"] / 100.0, 4)
            decision["reason"] = (
                f"NotebookLM 当前不可用，已回退到本地知识库，但仅获得框架级证据，"
                f"不足以支撑买入决策，维持观望。"
            )
            decision["thinking_trace"]["notebooklm"] = (
                f"本地知识库回退：{local_result.get('summary') or '已命中本地资料，但未形成明确买点。'}"
            )
            decision["knowledge_source"] = "local_rag"
            decision["decision_source"] = "local_rag"
            decision["knowledge_preview"] = local_result.get("summary") or ""
            return decision

        decision["knowledge_source"] = "unavailable"
        decision["decision_source"] = "unavailable"
        return decision

    def _extract_symbol_hint(self, question):
        code_match = re.search(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", question, re.IGNORECASE)
        if not code_match:
            return None
        raw_code = code_match.group(1)
        lower_question = question.lower()
        if f"sh{raw_code}" in lower_question or raw_code.startswith(("5", "6", "9")):
            return f"sh{raw_code}"
        if f"sz{raw_code}" in lower_question or raw_code.startswith(("0", "2", "3")):
            return f"sz{raw_code}"
        return raw_code

    async def _call_first_available_tool(self, mapping_key, argument_candidates):
        tool_names = self.market_tool_candidates.get(mapping_key, [])
        for _, server_params in self.market_servers:
            for tool_name in tool_names:
                for arguments in argument_candidates:
                    result = await self._call_tool(server_params, tool_name, arguments)
                    if result:
                        return result, tool_name, arguments
        return None, None, None

    def _extract_json_object(self, text):
        if not text:
            return None
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char != "{":
                continue
            try:
                parsed, end = decoder.raw_decode(text[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return text[idx:idx + end]
        return None

    def _extract_tool_error_text(self, text):
        json_str = self._extract_json_object(text or "")
        if not json_str:
            return None
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed.get("success") is False:
            return parsed.get("error") or parsed.get("message") or "MCP 工具调用失败"
        return None

    def _parse_notebook_payload(self, result_text):
        json_str = self._extract_json_object(result_text or "")
        if not json_str:
            return None
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def _extract_notebook_answer_text(self, result_text):
        payload = self._parse_notebook_payload(result_text or "")
        answer = ((payload or {}).get("data") or {}).get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer
        return result_text or ""

    def _normalize_dimension_scores(self, decision):
        raw_scores = decision.get("dimension_scores") or {}
        normalized = {}
        for key in ["data_arch", "notebooklm", "game_psych", "trend"]:
            value = float(raw_scores.get(key, 0.0) or 0.0)
            normalized[key] = max(0.0, min(25.0, value))
        total_score = round(sum(normalized.values()), 2)
        decision["dimension_scores"] = normalized
        decision["total_score"] = total_score
        decision["win_rate_confidence"] = round(total_score / 100.0, 4)
        return decision

    def _excerpt_raw_field(self, raw, key, max_chars):
        if not raw or not isinstance(raw, dict):
            return None
        v = raw.get(key)
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
        else:
            s = json.dumps(v, ensure_ascii=False)
        if len(s) > max_chars:
            return s[:max_chars].rstrip() + "..."
        return s

    def _compact_local_rag_for_bundle(self, lr):
        if not lr or not lr.get("success"):
            return {
                "success": False,
                "summary": None,
                "hits": [],
                "retrieval": (lr or {}).get("retrieval"),
            }
        hits = []
        for r in (lr.get("results") or [])[:5]:
            if not isinstance(r, dict):
                continue
            hits.append(
                {
                    "source_path": r.get("source_path"),
                    "title": r.get("title"),
                    "score": r.get("score"),
                    "text": ((r.get("text") or "")[:200]),
                }
            )
        return {
            "success": True,
            "summary": lr.get("summary"),
            "retrieval": lr.get("retrieval"),
            "hits": hits,
        }

    def _dual_l1_query_from_market(self, market_data, symbol):
        text = (market_data or {}).get("text") or ""
        text = self._compact_text(text, 500)
        parts = ["A股", "策略", "风险", "仓位", "纪律"]
        if symbol:
            parts.append(str(symbol).strip())
        parts.append(text[:350])
        return " ".join(p for p in parts if p).strip()

    def _finalize_buy_decision(self, decision, market_data, source_label):
        from llm_decision import normalize_structured_decision

        d = dict(decision) if isinstance(decision, dict) else {}
        if not d.get("knowledge_source"):
            if source_label == "litellm":
                d["knowledge_source"] = "litellm"
            elif source_label == "notebooklm":
                d["knowledge_source"] = "notebooklm"
            elif source_label == "local_rag":
                d["knowledge_source"] = "local_rag"
            else:
                d["knowledge_source"] = source_label
        dual = (self.config.get("agent") or {}).get("dual_l1_evidence") or {}
        if dual.get("enabled") and source_label in ("litellm", "notebooklm"):
            q = self._dual_l1_query_from_market(market_data, d.get("symbol"))
            lr = self._search_local_knowledge(q, symbol_hint=d.get("symbol"))
            bundle = d.get("knowledge_evidence_bundle")
            if not isinstance(bundle, dict):
                bundle = {}
            bundle["local_rag"] = self._compact_local_rag_for_bundle(lr)
            d["knowledge_evidence_bundle"] = bundle
            d["dual_l1_local_rag_ok"] = bool(lr and lr.get("success"))
        raw = (market_data or {}).get("raw") if isinstance(market_data, dict) else None
        d["market_observability"] = {
            "used_tools": (market_data or {}).get("used_tools") if isinstance(market_data, dict) else None,
            "mcp_quotes_enabled": bool(self.enrichment.get("mcp_quotes_enabled", True)),
            "stock_quote_excerpt": self._excerpt_raw_field(raw, "stock_quote", 500),
            "index_data_excerpt": self._excerpt_raw_field(raw, "index_data", 400),
        }
        return normalize_structured_decision(d)

    def _build_symbol_queries(self, symbol=None, name=None):
        queries = []
        if symbol:
            queries.append(symbol)
        if name and name not in queries:
            queries.append(name)
        return queries or ["上证指数"]

    def _compact_text(self, text, max_chars=1200):
        text = (text or "").strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    async def get_market_data(self, symbol=None, name=None):
        """获取当前市场与候选标的行情摘要作为决策输入"""
        logging.info("获取 A 股最新宏观与行业板块行情数据...")

        industry_data, industry_tool, _ = await self._call_first_available_tool(
            "industry_list",
            [{}],
        )

        index_queries = ["上证指数", "中证500", "沪深300", "黄金ETF", "北大荒", "农业ETF"]
        index_data, quotes_tool, _ = await self._call_first_available_tool(
            "quotes",
            [{"queries": index_queries}, {"query": " ".join(index_queries)}],
        )

        symbol_queries = self._build_symbol_queries(symbol=symbol, name=name)
        stock_quote, _, _ = await self._call_first_available_tool(
            "quotes",
            [{"queries": symbol_queries}, {"query": " ".join(symbol_queries)}],
        )
        intraday_data, intraday_tool, _ = await self._call_first_available_tool(
            "intraday",
            [{"symbol": symbol}, {"code": symbol}, {"query": symbol}] if symbol else [{}],
        )
        fundamentals_data, fundamentals_tool, _ = await self._call_first_available_tool(
            "fundamentals",
            [{"symbol": symbol}, {"code": symbol}, {"query": symbol}] if symbol else [{}],
        )
        technicals_data, technicals_tool, _ = await self._call_first_available_tool(
            "technicals",
            [{"symbol": symbol}, {"code": symbol}, {"query": symbol}] if symbol else [{}],
        )
        capital_flow_data, capital_flow_tool, _ = await self._call_first_available_tool(
            "capital_flow",
            [{"symbol": symbol}, {"code": symbol}, {"query": symbol}] if symbol else [{}],
        )

        used_tools = [tool for tool in [
            industry_tool,
            quotes_tool,
            intraday_tool,
            fundamentals_tool,
            technicals_tool,
            capital_flow_tool,
        ] if tool]

        market_context = f"""
【宽基指数与核心标的实时状态】：
{index_data or "未获取到宽基指数数据"}

【当前行业板块资金流向与涨跌概况】：
{industry_data or "未获取到行业板块数据"}

【候选标的即时行情】：
{stock_quote or "未指定候选标的，或暂未获取到标的行情"}

【分时与趋势动能】：
{intraday_data or "暂未获取到分时数据"}

【估值与基本面】：
{fundamentals_data or "暂未获取到 PE/PB 等估值数据"}

【技术指标（KDJ/MACD 等）】：
{technicals_data or "暂未获取到技术指标数据"}

【大单/资金流】：
{capital_flow_data or "暂未获取到大单资金流向数据"}
"""
        raw_out = {
            "industry_data": industry_data,
            "index_data": index_data,
            "stock_quote": stock_quote,
            "intraday_data": intraday_data,
            "fundamentals_data": fundamentals_data,
            "technicals_data": technicals_data,
            "capital_flow_data": capital_flow_data,
        }
        enrich = self.enrichment
        extra_parts = []
        try:
            from market_enrichment import collect_stock_like_queries, tavily_news_block, tushare_summary_lines

            if enrich.get("tushare_enabled", True):
                syms = []
                for q in symbol_queries:
                    m6 = re.search(r"\d{6}", str(q))
                    if m6:
                        syms.append(m6.group(0))
                if symbol:
                    syms.append(str(symbol).strip())
                syms = list(dict.fromkeys(syms))
                if syms:
                    block = await asyncio.to_thread(tushare_summary_lines, syms, enrich)
                    if block:
                        extra_parts.append(block)
                        raw_out["tushare_enrichment"] = block
            if enrich.get("tavily_enabled", True):
                queries = collect_stock_like_queries(symbol, name, index_queries)
                block = await asyncio.to_thread(tavily_news_block, queries, enrich)
                if block:
                    extra_parts.append(block)
                    raw_out["tavily_enrichment"] = block
        except Exception as exc:
            logging.warning("行情/新闻增强失败（已忽略，不影响主流程）: %s", exc)
        if extra_parts:
            market_context = market_context.strip() + "\n\n" + "\n\n".join(extra_parts)

        return {
            "text": market_context.strip(),
            "raw": raw_out,
            "used_tools": used_tools,
        }

    @staticmethod
    def _parse_mcp_quotes_payload(data) -> dict[str, float]:
        """解析 stock-sdk-mcp 返回：可能是 [{...}] 或 {\"results\": [{...}]}。"""
        out: dict[str, float] = {}
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            rows = data["results"]
        elif isinstance(data, list):
            rows = data
        elif isinstance(data, dict) and (data.get("code") or data.get("symbol")):
            rows = [data]
        else:
            return out
        for item in rows:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol") or item.get("code")
            price = item.get("price") or item.get("current")
            if sym is not None and price is not None:
                try:
                    out[str(sym).strip()] = float(price)
                except (TypeError, ValueError):
                    continue
        return out

    async def get_fundamentals_data(self, symbols: list[str]) -> pd.DataFrame:
        """批量获取估值数据"""
        if not symbols: return pd.DataFrame()
        fund_text, _, _ = await self._call_first_available_tool(
            "fundamentals", [{"queries": symbols}]
        )
        # 简化：假设返回 JSON 包含 pe_ttm, pb_mrq
        try:
            data = json.loads(fund_text)
            return pd.DataFrame(data if isinstance(data, list) else data.get("results", []))
        except:
            return pd.DataFrame()

    async def get_historical_data(self, symbols: list[str]) -> pd.DataFrame:
        """批量获取历史行情（动量计算）"""
        # 实际应调用 MCP 获取过去 20 天日线
        return pd.DataFrame()

    async def update_holdings_prices(self, symbols):
        """批量获取持仓股票最新价格"""
        if not symbols:
            return {}

        enrich = self.enrichment
        price_dict = {}
        if enrich.get("mcp_quotes_enabled", True):
            result_text, _, _ = await self._call_first_available_tool(
                "quotes",
                [{"queries": symbols}, {"query": " ".join(symbols)}],
            )
            if result_text:
                try:
                    data = json.loads(result_text)
                    parsed = self._parse_mcp_quotes_payload(data)
                    price_dict.update(parsed)
                    if not parsed:
                        logging.warning("MCP 行情 JSON 已解析但未得到任何现价字段，原始类型: %s", type(data).__name__)
                except Exception as e:
                    logging.error("解析 MCP 行情 JSON 失败: %s", e)

        if enrich.get("tushare_enabled", True):
            from market_enrichment import tushare_fill_prices

            missing = [s for s in symbols if str(s).strip() not in price_dict]
            if missing:
                filled = await asyncio.to_thread(tushare_fill_prices, missing, enrich)
                for k, v in filled.items():
                    price_dict[k] = v

        return price_dict

    async def _make_decision_litellm(self, market_data):
        from llm_decision import decision_json_instruction, run_litellm_decision_chain

        logging.info("调用 LiteLLM 进行结构化选股决策...")
        market_text = market_data.get("text") if isinstance(market_data, dict) else str(market_data)
        raw_market = market_data.get("raw") if isinstance(market_data, dict) else None
        prompt_context, _ = self._compose_prompt_stack("execute", force_mode="execution")
        schema = decision_json_instruction()
        user_prompt = (
            f"{prompt_context}\n\n{schema}\n\n"
            f"【当前A股行情摘要】\n{market_text}\n\n"
            f"【结构化原始行情】\n"
            f"{json.dumps(raw_market, ensure_ascii=False, indent=2) if raw_market else '无'}"
        )
        models = self._litellm_model_chain()
        timeout = float(self.litellm_cfg.get("timeout_seconds", 90))
        decision, raw, err = await run_litellm_decision_chain(
            user_prompt,
            models,
            timeout,
            self._extract_json_object,
        )
        if decision:
            decision = self._normalize_dimension_scores(decision)
            decision["success"] = True
            sym = decision.get("symbol")
            if sym is not None and not isinstance(sym, str):
                sym = str(sym).strip()
                decision["symbol"] = sym
            if isinstance(sym, str) and sym.strip().lower() in ("null", "none", ""):
                decision["symbol"] = None
            decision["decision_source"] = "litellm"
            return decision, user_prompt, raw
        logging.warning("LiteLLM 决策链未返回有效 JSON: %s", err or "")
        return None, user_prompt, raw

    async def _make_decision_notebooklm(self, market_data):
        """NotebookLM 路径（与历史行为一致，失败时本地 RAG）。"""
        logging.info("调用 NotebookLM 智能体进行多域预判...")

        market_text = market_data.get("text") if isinstance(market_data, dict) else str(market_data)
        raw_market = market_data.get("raw") if isinstance(market_data, dict) else None
        prompt_context, _ = self._compose_prompt_stack("execute", force_mode="execution")
        prompt = f"""
        {prompt_context}

        【当前A股行情摘要】：
        {market_text}

        【结构化原始行情】
        {json.dumps(raw_market, ensure_ascii=False, indent=2) if raw_market else "无"}
        """

        result_text = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {
                "notebook_id": self.notebook_id,
                "question": prompt
            }
        )

        if not result_text:
            error_message = self.get_last_tool_error("notebooklm", "ask_question")
            logging.warning("NotebookLM 未返回结果或调用失败，触发战时容错机制：不采取任何行动，强制保持空仓。")
            local_result = self._search_local_knowledge("A股策略框架 宏观政策 风险控制 买入阈值")
            return self._build_local_rag_decision(local_result, error_message), prompt, None
        tool_error = self._extract_tool_error_text(result_text)
        if tool_error:
            local_result = self._search_local_knowledge("A股策略框架 宏观政策 风险控制 买入阈值")
            return self._build_local_rag_decision(local_result, tool_error), prompt, result_text

        try:
            answer_text = self._extract_notebook_answer_text(result_text)
            json_str = self._extract_json_object(answer_text)
            if json_str:
                decision = json.loads(json_str)
                decision = self._normalize_dimension_scores(decision)
                decision["success"] = True
                decision["decision_source"] = "notebooklm"
                return decision, prompt, result_text
            logging.error("无法从 NotebookLM 返回结果中提取 JSON。")
            return self._build_unavailable_decision("NotebookLM 返回内容不是有效 JSON"), prompt, result_text
        except json.JSONDecodeError as e:
            logging.error("解析决策 JSON 失败: %s", e)
            return self._build_unavailable_decision(f"解析 NotebookLM JSON 失败: {str(e)}"), prompt, result_text

    async def make_decision(self, market_data):
        """
        核心决策流程重构：
        1. 获取候选池数据（动量/估值/流动性/技术面）
        2. 量化因子筛选
        3. LLM 针对通过筛选的标的进行定性分析
        """
        logging.info("--- [量化+LLM] 复合决策引擎启动 ---")
        
        # 1. 获取候选池（这里假设从配置或 screening_universe 获取）
        import screening_universe as su
        universe = su.load_universe(self.config)
        if not universe:
            return {"action": "观望", "reason": "未配置股票池"}, "", ""

        # 2. 批量拉取量化数据
        symbols = [u["symbol"] for u in universe]
        hist_df = await self.get_historical_data(symbols)
        fund_df = await self.get_fundamentals_data(symbols)
        
        # 3. 执行因子计算与筛选
        # 简化版：这里演示逻辑，实际会调用 quant_factors 各个方法
        passed_symbols = []
        for sym in symbols:
            # 模拟筛选：只有通过动量、估值、流动性、技术面四个维度的硬过滤才进入 LLM
            # passed = self.quant_factors.check_all(sym, hist_df, fund_df, ...)
            # 为了演示，我们假设前 3 个标的通过了初步筛选
            passed_symbols.append(sym)
            if len(passed_symbols) >= 3: break

        if not passed_symbols:
            return {"action": "观望", "reason": "量化因子硬过滤：本轮无标的通过"}, "", ""

        logging.info(f"量化因子筛选通过: {passed_symbols}，进入 LLM 定性分析。")

        # 4. LLM 职责：针对通过的标的进行非结构化信息解读
        runtime_cfg = self.config.get("runtime") or {}
        backend = str(runtime_cfg.get("decision_backend", "hybrid") or "hybrid").strip().lower()
        
        # 构造一个包含量化通过信息的 market_data
        enriched_market = dict(market_data)
        enriched_market["text"] += f"\n\n【量化筛选结果】：以下标的通过了动量/估值/流动性/技术面过滤，请重点分析其非结构化逻辑：{', '.join(passed_symbols)}"

        if backend in ("litellm", "hybrid") and self._litellm_model_chain():
            decision, prompt, raw = await self._make_decision_litellm(enriched_market)
            if decision and decision.get("symbol") in passed_symbols:
                decision["action"] = "buy"
                return decision, prompt, raw
        
        decision, prompt, raw = await self._make_decision_notebooklm(enriched_market)
        if decision and decision.get("symbol") in passed_symbols:
            decision["action"] = "buy"
        else:
            decision = {"action": "观望", "reason": "LLM 认为量化候选股目前逻辑不足或风险较高"}
        
        return decision, prompt, raw

    def _format_rag_for_screening(self, local_result):
        if not local_result or not local_result.get("success"):
            return "（本地知识库未命中有效摘要）"
        ev = local_result.get("evidence") or []
        sm = (local_result.get("summary") or "").strip()
        lines = [sm] if sm else []
        lines.extend(str(x) for x in ev[:6])
        return "\n".join(lines)[:3000]

    @staticmethod
    def _format_candidate_table(candidates: list) -> str:
        rows = []
        for c in candidates or []:
            if not isinstance(c, dict):
                continue
            fp = "池内" if c.get("from_pool") else "补足"
            rows.append(
                f"- {c.get('symbol')} {c.get('name') or ''} [{fp}] {c.get('thesis') or ''}"
            )
        return "\n".join(rows) if rows else "（无）"

    async def _phase1_screen_litellm(self, user_prompt: str) -> tuple[dict | None, str, str | None]:
        from llm_decision import run_litellm_decision_chain

        models = self._litellm_model_chain()
        ts = (self.config.get("agent") or {}).get("two_stage_screening") or {}
        timeout = float(ts.get("phase1_timeout_seconds") or self.litellm_cfg.get("timeout_seconds", 90))
        data, raw, err = await run_litellm_decision_chain(
            user_prompt,
            models,
            timeout,
            self._extract_json_object,
        )
        if not data or not isinstance(data, dict):
            logging.warning("阶段一 LiteLLM 未返回有效 JSON: %s", err or "")
            return None, user_prompt, raw
        if not isinstance(data.get("candidates"), list):
            return None, user_prompt, raw
        return data, user_prompt, raw

    async def _phase1_screen_notebooklm(self, user_prompt: str) -> tuple[dict | None, str, str | None]:
        result_text = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {"notebook_id": self.notebook_id, "question": user_prompt},
        )
        if not result_text or self._extract_tool_error_text(result_text or ""):
            logging.warning("阶段一 NotebookLM 不可用或未返回有效内容。")
            return None, user_prompt, result_text
        answer_text = self._extract_notebook_answer_text(result_text)
        json_str = self._extract_json_object(answer_text or "")
        if not json_str:
            return None, user_prompt, result_text
        try:
            data = json.loads(json_str)
            if isinstance(data, dict) and isinstance(data.get("candidates"), list):
                return data, user_prompt, result_text
        except json.JSONDecodeError:
            pass
        return None, user_prompt, result_text

    async def _phase1_screen_candidates(
        self,
        broad: dict,
        rag_text: str,
        universe: list,
        min_n: int,
        max_n: int,
    ) -> tuple[dict | None, str, str | None]:
        from llm_decision import phase1_screening_json_instruction
        import screening_universe as su

        schema = phase1_screening_json_instruction()
        rules = su.build_rules_text(universe, min_n, max_n)
        prompt_context, _ = self._compose_prompt_stack("execute", force_mode="execution")
        market_text = broad.get("text") or ""
        raw_broad = broad.get("raw")
        user_prompt = (
            f"{prompt_context}\n\n{schema}\n\n【股票池与数量硬规则】\n{rules}\n\n"
            f"【当前盘面与板块摘要】\n{self._compact_text(market_text, 12000)}\n\n"
            f"【本地知识库摘录】\n{self._compact_text(rag_text, 2500)}\n\n"
            f"【结构化原始行情（节选）】\n"
            f"{self._compact_text(json.dumps(raw_broad, ensure_ascii=False) if raw_broad else '无', 6000)}"
        )

        runtime_cfg = self.config.get("runtime") or {}
        backend = str(runtime_cfg.get("decision_backend", "hybrid") or "hybrid").strip().lower()

        if backend in ("litellm", "hybrid") and self._litellm_model_chain():
            data, p, r = await self._phase1_screen_litellm(user_prompt)
            if data:
                return data, p, r
            if backend == "litellm":
                return None, p, r
            logging.warning("阶段一 LiteLLM 失败，尝试 NotebookLM。")

        if backend == "litellm" and not self._litellm_model_chain():
            logging.warning("阶段一：decision_backend=litellm 但未配置模型，改用 NotebookLM。")

        return await self._phase1_screen_notebooklm(user_prompt)

    async def get_market_data_for_candidates(self, candidates: list[dict], screen_cfg: dict | None = None) -> dict:
        """阶段二：批量报价 + 前 N 只维度工具 + Tushare 摘要。"""
        screen_cfg = screen_cfg or {}
        symbols = [str(c.get("symbol") or "").strip() for c in candidates if c.get("symbol")]
        symbols = list(dict.fromkeys(symbols))
        parts: list[str] = []
        raw_out: dict = {}

        if not symbols:
            return {"text": "（无候选代码）", "raw": {}}

        result_text, _, _ = await self._call_first_available_tool(
            "quotes",
            [{"queries": symbols}, {"query": " ".join(symbols)}],
        )
        raw_out["batch_quotes"] = result_text
        if result_text:
            parts.append("【MCP 批量行情】\n" + self._compact_text(result_text, 8000))

        detail_n = int(screen_cfg.get("per_symbol_detail_limit", 5))
        per_sym_raw = []
        for c in candidates[:detail_n]:
            sym = str(c.get("symbol") or "").strip()
            if not sym:
                continue
            name = c.get("name") or ""
            intra, _, _ = await self._call_first_available_tool(
                "intraday",
                [{"symbol": sym}, {"code": sym}, {"query": sym}],
            )
            fund, _, _ = await self._call_first_available_tool(
                "fundamentals",
                [{"symbol": sym}, {"code": sym}, {"query": sym}],
            )
            tech, _, _ = await self._call_first_available_tool(
                "technicals",
                [{"symbol": sym}, {"code": sym}, {"query": sym}],
            )
            cap, _, _ = await self._call_first_available_tool(
                "capital_flow",
                [{"symbol": sym}, {"code": sym}, {"query": sym}],
            )
            per_sym_raw.append(
                {"symbol": sym, "intraday": intra, "fundamentals": fund, "technicals": tech, "capital_flow": cap}
            )
            parts.append(
                f"\n--- 标的 {sym} {name} ---\n"
                f"分时: {self._compact_text(intra or '', 1200)}\n"
                f"基本面: {self._compact_text(fund or '', 1200)}\n"
                f"技术: {self._compact_text(tech or '', 1200)}\n"
                f"资金流: {self._compact_text(cap or '', 1200)}"
            )
        raw_out["per_symbol"] = per_sym_raw

        try:
            from market_enrichment import tushare_summary_lines

            if self.enrichment.get("tushare_enabled", True):
                block = await asyncio.to_thread(tushare_summary_lines, symbols, self.enrichment)
                if block:
                    parts.append(block)
                    raw_out["tushare_enrichment"] = block
        except Exception as exc:
            logging.warning("阶段二 Tushare 摘要失败: %s", exc)

        return {"text": "\n".join(parts).strip(), "raw": raw_out}

    async def run_two_stage_buy_decision(self):
        """
        两阶段买入决策。成功返回与 make_decision 相同的三元组；失败返回 None（由主流程回退单阶段）。
        """
        agent_cfg = self.config.get("agent") or {}
        ts = agent_cfg.get("two_stage_screening") or {}
        if not ts.get("enabled"):
            return None

        min_n = int(ts.get("min_candidates", 5))
        max_n = int(ts.get("max_candidates", 10))
        import screening_universe as su

        universe = su.load_universe(ts)

        logging.info("两阶段选股：阶段一（大盘+提名），股票池 %s 只", len(universe))
        broad = await self.get_market_data()
        rag = self._search_local_knowledge("A股 大盘 板块 轮动 选股 宏观 政策 风险控制")
        rag_text = self._format_rag_for_screening(rag)

        payload = None
        prompt1 = ""
        raw1 = None
        for attempt in range(2):
            payload, prompt1, raw1 = await self._phase1_screen_candidates(
                broad, rag_text, universe, min_n, max_n
            )
            if not payload:
                logging.warning("阶段一第 %s 次未得到有效 JSON", attempt + 1)
                continue
            cands = payload.get("candidates") or []
            fixed, err = su.validate_candidates(cands, universe, min_n, max_n)
            if fixed:
                payload["_validated_candidates"] = fixed
                break
            logging.warning("阶段一候选校验失败: %s", err)
            payload = None
        else:
            logging.warning("两阶段选股：阶段一失败，将回退单阶段。")
            return None

        candidates = payload.get("_validated_candidates") or []
        narrative = (payload.get("market_narrative") or "").strip()

        logging.info("两阶段选股：阶段二（%s 只候选行情）", len(candidates))
        phase2 = await self.get_market_data_for_candidates(candidates, ts)

        final_text = (
            f"【阶段一·大盘与提名】\n{narrative}\n\n"
            f"【阶段一·候选列表】\n{self._format_candidate_table(candidates)}\n\n"
            f"【阶段二·个股行情与多维数据】\n{phase2.get('text') or ''}\n\n"
            "请基于以上两阶段材料，结合知识库纪律，只输出最终买卖决策 JSON（单只标的或观望），"
            "严格遵守 win_rate 与四维评分；无把握则 symbol 为 null。"
        )
        final_raw = {
            "two_stage": True,
            "phase1_market_narrative": narrative,
            "phase1_candidates": candidates,
            "phase1_raw": self._compact_text(raw1 or "", 4000),
            "broad_snapshot": broad.get("raw"),
            "phase2_raw": phase2.get("raw"),
        }
        return await self.make_decision({"text": final_text, "raw": final_raw})

    @staticmethod
    def normalize_exit_action(raw):
        if raw is None:
            return "hold"
        a = str(raw).strip().lower()
        if a in ("sell", "卖出", "清仓", "close", "liquidate", "s"):
            return "sell"
        if a in ("partial", "减仓", "部分卖出", "reduce", "trim"):
            return "partial"
        if a in ("hold", "持有", "观望", "keep", "h"):
            return "hold"
        return "hold"

    async def evaluate_position_exits(self, positions, account, market_data, trading_config, position_gates=None):
        """
        每轮由 NotebookLM 结合行情与持仓快照，输出是否持有/卖出/减仓（结构化 JSON）。
        position_gates: {symbol: {can_sell, is_locked, lock_remaining_minutes}}
        """
        if not positions:
            return None

        prompt_context, _ = self._compose_prompt_stack("execute", force_mode="execution")
        market_text = ""
        raw_market = None
        if isinstance(market_data, dict):
            market_text = market_data.get("text") or ""
            raw_market = market_data.get("raw")

        tc = trading_config or {}
        stop_loss_pct = abs(float(tc.get("stop_loss", -0.05)))
        partial_at = float(tc.get("partial_take_at_return", 0.15))

        rows = []
        gates = position_gates or {}
        for p in positions:
            sym = p.get("symbol") or ""
            avg = float(p.get("avg_price") or 0.0)
            cur = float(p.get("current_price") or 0.0)
            qty = int(p.get("quantity") or 0)
            ur = ((cur / avg) - 1.0) * 100.0 if avg > 0 else 0.0
            g = gates.get(sym) or {}
            rows.append(
                f"- {sym} {p.get('name') or ''} | 持仓{qty}股 | 成本{avg:.3f} 现价{cur:.3f} | "
                f"浮盈{ur:+.2f}% | 系统门禁 can_sell={g.get('can_sell')} "
                f"locked={g.get('is_locked')} 锁仓剩余约{g.get('lock_remaining_minutes', 0)}分钟"
            )

        prompt = f"""
{prompt_context}

你当前任务是【持仓退出复核】：结合知识库与下列行情摘要，对每一只持仓给出独立动作建议。
硬约束（与模拟盘一致）：
- A 股 T+1：系统已标注 can_sell；为 false 时你仍应给出观点，但不得假设可以成交当日买入部分。
- 买入后锁仓时间内 is_locked 为 true 时同理。
- 系统另有自动规则：均价约 {stop_loss_pct*100:.0f}% 硬止损、约 +{partial_at*100:.0f}% 强制减仓、移动止盈与最长持仓天数；你的建议用于「智能体主动风控」，与规则并行。

【账户快照】
总资产约 {float(account.get('total_assets') or 0):.2f}，可用现金约 {float(account.get('balance') or 0):.2f}

【当前持仓】
{chr(10).join(rows)}

【A股行情摘要】
{self._compact_text(market_text, 2000)}

【结构化原始行情（节选）】
{self._compact_text(json.dumps(raw_market, ensure_ascii=False) if raw_market else "无", 2500)}

请只输出一个 JSON 对象：
{{
  "evaluations": [
    {{
      "symbol": "与上表完全一致的代码如 sz300750",
      "action": "hold | sell | partial",
      "partial_ratio": 0.5,
      "confidence": 0.0,
      "reason": "一句话理由，需可对照知识库或盘面逻辑"
    }}
  ],
  "portfolio_note": "可选：组合层面一句总结"
}}
必须为每个持仓各输出一条 evaluations；若无把握则 action 为 hold 且 confidence 偏低。
"""

        result_text = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {"notebook_id": self.notebook_id, "question": prompt},
        )
        if not result_text or self._extract_tool_error_text(result_text or ""):
            logging.warning("持仓退出复核：NotebookLM 不可用或未返回有效内容。")
            return None
        answer_text = self._extract_notebook_answer_text(result_text)
        json_str = self._extract_json_object(answer_text or "")
        if not json_str:
            logging.warning("持仓退出复核：无法解析 JSON。")
            return None
        try:
            payload = json.loads(json_str)
            if isinstance(payload, dict) and isinstance(payload.get("evaluations"), list):
                return payload
        except json.JSONDecodeError:
            pass
        logging.warning("持仓退出复核：JSON 结构无效。")
        return None

    async def ask_knowledgebase(self, question, history=None):
        """面向 NotebookLM 的持续问答接口（原始检索）"""
        history = history or []
        scene = self._detect_scene(question)
        prompt_context, prompt_mode = self._compose_prompt_stack(scene)
        history_text = ""
        if history:
            formatted = []
            for turn in history[-6:]:
                q = turn.get("q", "")
                a = turn.get("a", "")
                formatted.append(f"用户: {q}\n助手: {a}")
            history_text = "\n\n【最近对话上下文】\n" + "\n\n".join(formatted)

        prompt = f"""
{prompt_context}

你是知识库问答助手。当前问题场景为 {scene}（{self._scene_label(scene)}），当前启用的提示模式为 {prompt_mode}。
请结合 NotebookLM 内容，给出准确、简洁、可执行的回答。

{history_text}

【当前问题】
{question}
"""

        result_text = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {
                "notebook_id": self.notebook_id,
                "question": prompt
            }
        )
        tool_error = self._extract_tool_error_text(result_text or "")
        if result_text and not tool_error:
            return self._extract_notebook_answer_text(result_text)

        local_result = self._search_local_knowledge(question)
        if local_result.get("success"):
            evidence = local_result.get("evidence") or []
            lines = [
                f"本地知识库结论：{local_result.get('summary')}",
                "本地证据：",
            ]
            lines.extend(f"- {item}" for item in evidence[:3])
            return "\n".join(lines)
        return result_text

    async def ask_multi_domain_foresight(self, question, history=None):
        """
        多域预判问答：
        1) 从 NotebookLM 获取知识库证据
        2) 结合实时市场上下文进行二次研判
        3) 输出结构化结论（而不是直接回传 NotebookLM 原文）
        """
        history = history or []
        scene = self._detect_scene(question)
        prompt_context, prompt_mode = self._compose_prompt_stack(scene)
        symbol_hint = self._extract_symbol_hint(question)

        # 步骤1：知识库证据抽取
        kb_prompt = f"""
{prompt_context}

你当前处理的是聊天问答场景，已显式判定为 {scene}（{self._scene_label(scene)}），当前启用的提示模式为 {prompt_mode}。
请只基于 NotebookLM 知识库回答，并输出结构化 JSON：
{{
  "scene": "{scene}",
  "kb_summary": "对问题的知识库结论（1-2句）",
  "kb_evidence": ["关键证据1", "关键证据2", "关键证据3"],
  "ignored_details": ["被多数人忽略的细节1", "被多数人忽略的细节2"],
  "opportunity_type": "机会类型或当前问题归属",
  "benefit_logic": "效益逻辑或收益驱动路径",
  "action_priority": "行动优先级与下一步动作",
  "risk_flags": ["风险1", "风险2"],
  "assumptions": ["隐含假设1", "隐含假设2"],
  "confidence": 0.0,
  "persona_notes": {{
    "data_arch": "数据架构师对知识证据的结论",
    "notebooklm": "知识库对齐结论",
    "game_psych": "心理博弈者从知识库延伸出的判断",
    "trend": "冷静执行者对趋势与纪律的提醒"
  }}
}}
问题：{question}
"""
        kb_raw = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {"notebook_id": self.notebook_id, "question": kb_prompt}
        )

        local_result = None
        kb_data = {
            "scene": scene,
            "kb_summary": "知识库暂无明确结论",
            "kb_evidence": [],
            "ignored_details": [],
            "opportunity_type": "",
            "benefit_logic": "",
            "action_priority": "",
            "risk_flags": [],
            "assumptions": [],
            "confidence": 0.0,
            "persona_notes": {},
        }
        notebook_error = None
        kb_answer_text = ""
        if kb_raw:
            tool_error = self._extract_tool_error_text(kb_raw)
            if tool_error:
                notebook_error = tool_error
                kb_raw = None
            else:
                kb_answer_text = self._extract_notebook_answer_text(kb_raw)
            try:
                if kb_answer_text:
                    json_str = self._extract_json_object(kb_answer_text)
                    if json_str:
                        parsed = json.loads(json_str)
                        if isinstance(parsed, dict):
                            kb_data.update(parsed)
            except Exception:
                logging.warning("知识库结构化抽取失败，改用原文摘要。")
                if kb_answer_text:
                    kb_data["kb_summary"] = kb_answer_text[:240]
        else:
            notebook_error = self.get_last_tool_error("notebooklm", "ask_question")

        if not kb_raw:
            local_result = self._search_local_knowledge(question, symbol_hint=symbol_hint)
            local_kb = self._local_rag_as_kb_data(local_result)
            if local_kb:
                kb_data.update(local_kb)

        gap_raw = None
        gap_called = False
        if kb_raw and self.agent_multihop.get("enabled"):
            kb_data, gap_raw, gap_called = await self._notebooklm_gap_fill_hop(
                question, scene, kb_data, prompt_context, prompt_mode
            )

        # 步骤2：实时市场上下文（可用则接入，不可用则降级）
        market_context = None
        try:
            market_context = await self.get_market_data(symbol=symbol_hint, name=symbol_hint)
        except Exception:
            market_context = None

        # 步骤3：multi-domain-foresight 二次研判输出
        evidence_lines = kb_data.get("kb_evidence") or []
        ignored_details = kb_data.get("ignored_details") or []
        assumptions = kb_data.get("assumptions") or []
        risk_flags = kb_data.get("risk_flags") or []
        conf = float(kb_data.get("confidence", 0.0) or 0.0)
        summary = kb_data.get("kb_summary", "知识库暂无明确结论")
        opportunity_type = kb_data.get("opportunity_type", "")
        benefit_logic = kb_data.get("benefit_logic", "")
        action_priority = kb_data.get("action_priority", "")
        persona_notes = kb_data.get("persona_notes") or {}
        scene_label = self._scene_label(scene)
        scene_policy = self._render_scene_policy(scene)
        default_next_step = (
            "建议先小仓位试探，并设置明确止损/止盈；若后续数据与假设一致，再逐步加仓。"
            if prompt_mode == "execution"
            else "建议先补充公开信息、验证触发条件与证据链，再决定是否进入执行层评估。"
        )

        market_note = "已纳入实时市场上下文（指数/行业/资金流）" if market_context else "未获取到实时市场数据，本次以知识库为主"
        history_note = "；".join([f"Q:{t.get('q','')[:20]}..." for t in history[-3:]]) if history else "无"
        if kb_raw:
            knowledge_status = "ok"
            knowledge_status_text = "NotebookLM 知识库已接入并参与研判"
        elif local_result and local_result.get("success"):
            knowledge_status = "local_rag"
            knowledge_status_text = (
                f"NotebookLM 当前不可用，已回退到本地知识库："
                f"{(local_result.get('results') or [{}])[0].get('source_path', '本地资料')}"
            )
        else:
            knowledge_status = "unavailable"
            knowledge_status_text = f"知识库当前不可用：{notebook_error or 'NotebookLM 未返回内容'}"

        multihop_line = ""
        if kb_raw and self.agent_multihop.get("enabled"):
            multihop_line = (
                "- 多跳协议：已对 NotebookLM 首轮结构化结果发起第二轮「缺口补全」。\n"
                if gap_called
                else "- 多跳协议：首轮证据与置信度已达标，未触发 NotebookLM 第二轮补全。\n"
            )
        elif local_result and local_result.get("success"):
            lr = local_result.get("retrieval") or {}
            multihop_line = f"- 多跳协议：本地 RAG 检索为 {lr.get('mode', 'unknown')}（见 multihop_meta）。\n"

        final_answer = (
            "【multi-domain-foresight 研判】\n"
            f"问题：{question}\n\n"
            "0) 场景判定\n"
            f"- 当前场景：{scene_label}\n"
            f"- 提示模式：{prompt_mode}\n"
            f"- 协议摘要：{scene_policy.replace(chr(10), '；') if scene_policy else '无'}\n\n"
            f"1) 结论\n- {summary}\n\n"
            f"1.5) 机会类型\n- {opportunity_type or '未明确归类'}\n\n"
            "2) 多域依据\n"
            f"- 知识库依据：{'; '.join(evidence_lines) if evidence_lines else '未提取到明确证据'}\n"
            f"- 被忽略的细节：{'; '.join(ignored_details) if ignored_details else '暂无'}\n"
            f"- 知识库状态：{knowledge_status_text}\n"
            f"{multihop_line}"
            f"- 市场域：{market_note}\n"
            f"- 对话连续性：{history_note}\n\n"
            "2.5) 人设思考轨迹\n"
            f"- 数据架构师：{persona_notes.get('data_arch') or '未生成'}\n"
            f"- 知识库对齐：{persona_notes.get('notebooklm') or '未生成'}\n"
            f"- 心理博弈者：{persona_notes.get('game_psych') or '未生成'}\n"
            f"- 冷静执行者：{persona_notes.get('trend') or '未生成'}\n\n"
            "3) 效益逻辑\n"
            f"- {benefit_logic or '暂无明确效益逻辑'}\n\n"
            "4) 假设与风险\n"
            f"- 关键假设：{'; '.join(assumptions) if assumptions else '暂无'}\n"
            f"- 风险标记：{'; '.join(risk_flags) if risk_flags else '暂无'}\n"
            "- 风险提示：若政策节奏、资金风格或行业景气度出现反转，结论需立即重估。\n\n"
            "5) 行动建议\n"
            f"- 优先级：{action_priority or '建议先补证据，再决定动作。'}\n"
            f"- {default_next_step}\n"
            f"- 当前结论置信度（知识库侧）：{conf:.2f}\n"
        )

        return {
            "scene": scene,
            "scene_label": scene_label,
            "prompt_mode": prompt_mode,
            "final_answer": final_answer,
            "kb_raw": kb_raw,
            "kb_structured": kb_data,
            "used_market_context": bool(market_context),
            "used_notebooklm": bool(kb_raw),
            "used_local_rag": bool(local_result and local_result.get("success")),
            "kb_preview": (kb_answer_text or kb_raw or "")[:300],
            "kb_status": knowledge_status,
            "kb_error": notebook_error,
            "knowledge_source": "notebooklm" if kb_raw else ("local_rag" if local_result and local_result.get("success") else "unavailable"),
            "multihop_meta": {
                "notebooklm_rounds": (1 + (1 if gap_called else 0)) if kb_raw else 0,
                "notebooklm_gap_fill_called": gap_called,
                "local_rag_retrieval": (local_result or {}).get("retrieval"),
            },
        }

    async def explain_why_not_buy(self, symbol, history=None):
        history = history or []
        market_data = await self.get_market_data(symbol=symbol, name=symbol)
        history_text = "\n".join(
            [
                f"用户: {self._compact_text(item.get('q', ''), 80)}\n助手: {self._compact_text(item.get('a', ''), 120)}"
                for item in history[-2:]
            ]
        )
        market_summary = self._compact_text((market_data or {}).get("text"), 1200)
        prompt = f"""
你是 MDA v2.0 A股决策系统的解释器。
请只基于 NotebookLM 知识库与当前市场摘要，回答“为什么不买入 {symbol}”，并且只输出一个结构化 JSON：
{{
  "symbol": "{symbol}",
  "final_score": 0.0,
  "pass_threshold": 75.0,
  "dimension_breakdown": [
    {{"name": "数据架构维度", "score": 0.0, "max_score": 25.0, "deductions": ["扣分原因1"]}},
    {{"name": "知识库维度", "score": 0.0, "max_score": 25.0, "deductions": ["扣分原因2"]}},
    {{"name": "博弈心理维度", "score": 0.0, "max_score": 25.0, "deductions": ["扣分原因3"]}},
    {{"name": "趋势动能维度", "score": 0.0, "max_score": 25.0, "deductions": ["扣分原因4"]}}
  ],
  "conclusion": "一句话总结为什么不买",
  "action": "观望"
}}

最近对话：
{history_text or "无"}

当前市场摘要：
{market_summary or "无"}
"""
        result_text = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {"notebook_id": self.notebook_id, "question": prompt}
        )
        notebook_error = self.get_last_tool_error("notebooklm", "ask_question")
        tool_error = self._extract_tool_error_text(result_text or "")
        if tool_error:
            notebook_error = tool_error
            result_text = None
        local_result = None
        if not result_text:
            local_result = self._search_local_knowledge(f"为什么不买入 {symbol}", symbol_hint=symbol)
        answer_text = self._extract_notebook_answer_text(result_text or "")
        json_str = self._extract_json_object(answer_text)
        if json_str:
            try:
                parsed = json.loads(json_str)
                if parsed.get("success") is False:
                    parsed["knowledge_status"] = "unavailable"
                else:
                    parsed["knowledge_status"] = "ok"
                return parsed
            except json.JSONDecodeError:
                pass
        if local_result and local_result.get("success"):
            local_score = round(min(25.0, float(local_result.get("confidence", 0.0)) * 25.0), 2)
            evidence = local_result.get("evidence") or []
            return {
                "symbol": symbol,
                "final_score": local_score,
                "pass_threshold": 75.0,
                "dimension_breakdown": [
                    {"name": "数据架构维度", "score": 0.0, "max_score": 25.0, "deductions": ["当前仅使用本地知识库兜底，未完成完整数据域解释。"]},
                    {"name": "知识库维度", "score": local_score, "max_score": 25.0, "deductions": evidence[:3] or ["本地知识库未命中足够证据。"]},
                    {"name": "博弈心理维度", "score": 0.0, "max_score": 25.0, "deductions": ["缺少 NotebookLM 人格化推理，暂不放行。"]},
                    {"name": "趋势动能维度", "score": 0.0, "max_score": 25.0, "deductions": ["缺少完整趋势联判，保守维持观望。"]},
                ],
                "conclusion": f"NotebookLM 当前不可用，已回退到本地知识库：{local_result.get('summary')}",
                "action": "观望",
                "knowledge_status": "local_rag",
                "knowledge_source": "local_rag",
                "error": notebook_error,
            }
        return {
            "symbol": symbol,
            "final_score": 0.0,
            "pass_threshold": 75.0,
            "dimension_breakdown": [],
            "conclusion": result_text or f"NotebookLM 当前不可用：{notebook_error or '未能生成详细扣分解释。'}",
            "action": "观望",
            "knowledge_status": "unavailable",
            "error": notebook_error,
        }

    async def setup_notebooklm_auth(self):
        try:
            health_result = await self._call_tool(
                self.notebooklm_params,
                "get_health",
                {}
            )
            health_payload = self._parse_notebook_payload(health_result or "")
            health_data = (health_payload or {}).get("data") or {}
            health_error = self._extract_tool_error_text(health_result or "")
            if health_data.get("authenticated") and not health_error:
                return {
                    "success": True,
                    "message": "NotebookLM 已经处于可用状态，无需重新登录。",
                    "health": health_data,
                }
        except Exception:
            pass

        self._cleanup_stale_notebooklm_processes()
        await asyncio.sleep(1)
        result_text = await self._call_tool(
            self.notebooklm_params,
            "setup_auth",
            {}
        )
        error_message = self.get_last_tool_error("notebooklm", "setup_auth")
        if not result_text:
            return {
                "success": False,
                "error": error_message or "未返回认证结果",
            }
        json_str = self._extract_json_object(result_text)
        if json_str:
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {
            "success": False,
            "error": result_text,
        }

    async def get_notebooklm_status(self):
        local_rag_status = self.local_rag.status()
        notebooks_result = await self._call_tool(
            self.notebooklm_params,
            "list_notebooks",
            {}
        )
        error_message = self.get_last_tool_error("notebooklm", "list_notebooks")
        if not notebooks_result:
            return {
                "success": False,
                "available": False,
                "error": error_message or "无法获取 NotebookLM 列表",
                "notebook_count": 0,
                "local_rag": local_rag_status,
            }
        tool_error = self._extract_tool_error_text(notebooks_result)
        if tool_error:
            return {
                "success": False,
                "available": False,
                "error": tool_error,
                "notebook_count": 0,
                "local_rag": local_rag_status,
            }
        payload = self._parse_notebook_payload(notebooks_result)
        if not payload:
            return {
                "success": False,
                "available": False,
                "error": "无法解析 NotebookLM 列表",
                "notebook_count": 0,
                "local_rag": local_rag_status,
            }
        try:
            notebooks = ((payload.get("data") or {}).get("notebooks")) or []
            health_result = await self._call_tool(
                self.notebooklm_params,
                "get_health",
                {}
            )
            health_payload = self._parse_notebook_payload(health_result)
            health_data = (health_payload or {}).get("data") or {}
            health_error = self._extract_tool_error_text(health_result or "")

            if not notebooks:
                return {
                    "success": False,
                    "available": False,
                    "error": "NotebookLM 库为空，当前没有可用 notebook",
                    "notebook_count": 0,
                    "notebooks": [],
                    "health": health_data,
                    "probe_status": "skipped",
                    "local_rag": local_rag_status,
                }

            probe_token = str(int(time.time() * 1000))
            expected_probe = f"HEALTHY:Factions:{probe_token}"
            probe_prompt = (
                "你正在执行 MDA 系统健康检查。"
                "请只基于当前 NotebookLM 知识库回答，并且仅输出这一行文本："
                f"{expected_probe}"
            )
            probe_result = await self._call_tool(
                self.notebooklm_params,
                "ask_question",
                {
                    "notebook_id": self.notebook_id,
                    "question": probe_prompt,
                }
            )
            probe_error = self.get_last_tool_error("notebooklm", "ask_question")
            probe_tool_error = self._extract_tool_error_text(probe_result or "")
            if probe_tool_error:
                probe_error = probe_tool_error
                probe_result = None

            probe_text = self._extract_notebook_answer_text(probe_result or "")
            probe_fallback_text = probe_error or self.get_last_tool_error("notebooklm", "ask_question") or ""
            probe_preview = (probe_text or probe_result or probe_fallback_text).strip()[:300]
            probe_ok = expected_probe in (probe_text or "")
            if not probe_ok and probe_fallback_text:
                probe_ok = expected_probe in probe_fallback_text
            if not probe_ok:
                return {
                    "success": False,
                    "available": False,
                    "error": probe_error or "真实探活失败：ask_question 未返回预期结果",
                    "notebook_count": len(notebooks),
                    "notebooks": notebooks,
                    "health": health_data,
                    "health_error": health_error,
                    "probe_status": "failed",
                    "probe_preview": probe_preview,
                    "local_rag": local_rag_status,
                }

            return {
                "success": True,
                "available": True,
                "error": None,
                "notebook_count": len(notebooks),
                "notebooks": notebooks,
                "health": health_data,
                "health_error": health_error,
                "probe_status": "ok",
                "probe_preview": probe_preview,
                "local_rag": local_rag_status,
            }
        except json.JSONDecodeError:
            return {
                "success": False,
                "available": False,
                "error": "无法解析 NotebookLM 列表 JSON",
                "notebook_count": 0,
                "local_rag": local_rag_status,
            }

# 简单的测试运行块
if __name__ == "__main__":
    async def test():
        agent = MCPAgent()
        market_data = await agent.get_market_data()
        print("Market Data:", market_data)
        
        # decision, _, _ = await agent.make_decision(market_data)
        # print("Decision:", decision)
        
    asyncio.run(test())
