import yaml
import os
import logging
from datetime import datetime
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
import json
from openai import OpenAI

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class RiskGate:
    def __init__(self, config=None, config_path="config.yaml", qdrant_client=None, embedding_model=None):
        if config:
            self.config = config
        else:
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f)
        
        self.llm_decision_flow = self.config.get("llm_decision_flow", {})
        self.local_rag_cfg = self.config.get("local_rag", {})
        self.kb_update_cfg = self.config.get("knowledge_base_update", {})

        self.require_notebooklm_for_buy = self.llm_decision_flow.get("require_notebooklm_for_buy", False)
        self.primary_llm = self.llm_decision_flow.get("primary_llm", "notebooklm")
        self.fallback_llm = self.llm_decision_flow.get("fallback_llm", "local_rag")
        self.pure_quant_fallback_enabled = self.llm_decision_flow.get("pure_quant_fallback_enabled", True)

        self.collection_name = "knowledge_base"
        self.openai_client = OpenAI() # Initialize OpenAI client for RAG decision

        # 使用传入的 Qdrant 客户端和 Embedding 模型，而不是重新初始化
        self.qdrant_client = qdrant_client
        self.embedding_model = embedding_model

    def _query_local_rag(self, query_text: str, top_k: int = 3):

        if self.qdrant_client is None or self.embedding_model is None:
            logging.error("Qdrant client or embedding model not initialized for local RAG.")
            return []

        try:
            query_vector = self.embedding_model.encode(query_text).tolist()
            logging.info(f"Type of self.qdrant_client before search: {type(self.qdrant_client)}")
            logging.info(f"Methods available on self.qdrant_client: {dir(self.qdrant_client)}")
            search_result = self.qdrant_client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=self.llm_decision_flow.get("rag_retrieval_limit", top_k),
                query_filter=None # 暂时不使用过滤器，避免日期格式问题
            )
            return [hit.payload for hit in search_result]
        except Exception as e:
            logging.error(f"Error querying local RAG: {e}")
            return []

    def _make_rag_decision_with_llm(self, query: str, retrieved_docs: list[dict]) -> dict:
        """使用 LLM 结合检索到的文档进行决策"""
        if not retrieved_docs:
            return {"decision_source": "local_rag", "success": False, "reason": "No relevant documents found in local RAG.", "confidence": 0.0}
        
        context = "\n".join([doc.get("content", "") for doc in retrieved_docs])
        prompt = self.llm_decision_flow.get("retrieval_protocol_prompt", "")
        
        # 动态构建 LLM 提示，包含检索到的上下文和用户查询
        full_prompt = f"""
        {prompt}

        以下是相关知识库内容：
        {context}

        请根据上述信息，判断是否应该买入股票：{query}。请以 JSON 格式返回决策，包含 'success' (bool), 'reason' (str), 'confidence' (float, 0.0-1.0)。
        """
        
        try:
            response = self.openai_client.chat.completions.create(
                model=self.llm_decision_flow.get("rag_llm_model", "gemini-2.5-flash"), # 从 config 获取 RAG LLM 模型
                messages=[
                    {"role": "system", "content": "你是一个专业的量化投资决策助手，擅长结合研报和新闻进行投资判断。"},
                    {"role": "user", "content": full_prompt}
                ],
                response_format={"type": "json_object"}
            )
            llm_output = response.choices[0].message.content
            decision = json.loads(llm_output)
            decision["decision_source"] = "local_rag"
            return decision
        except Exception as e:
            logging.error(f"Error making RAG decision with LLM: {e}")
            return {"decision_source": "local_rag", "success": False, "reason": f"LLM decision failed: {e}", "confidence": 0.0}

    def buy_blocked_reason(self, stock_code: str, current_date: datetime, notebooklm_decision: dict = None) -> tuple[bool, str]:
        # 1. Primary LLM (NotebookLM) Decision
        if self.primary_llm == "notebooklm" and self.require_notebooklm_for_buy:
            if notebooklm_decision and notebooklm_decision.get("success"):
                logging.info(f"NotebookLM decision for {stock_code} on {current_date.strftime('%Y-%m-%d')}: {notebooklm_decision.get('reason')}")
                return not notebooklm_decision.get("allow_buy", False), notebooklm_decision.get("reason", "NotebookLM decision.")
            else:
                logging.warning(f"NotebookLM decision failed or not available for {stock_code} on {current_date.strftime('%Y-%m-%d')}. Falling back...")
                
        # 2. Fallback LLM (Local RAG) Decision
        if self.fallback_llm == "local_rag":
            query = f"分析股票 {stock_code} 在 {current_date.strftime('%Y-%m-%d')} 前后的新闻和研报，判断是否适合买入。"
            rag_results = self._query_local_rag(query)
            rag_decision = self._make_rag_decision_with_llm(query, rag_results)
            if rag_decision.get("success"):
                logging.info(f"Local RAG decision for {stock_code} on {current_date.strftime('%Y-%m-%d')}: {rag_decision.get('reason')}")
                return False, rag_decision.get("reason", "Local RAG decision.")
            else:
                logging.warning(f"Local RAG decision not conclusive for {stock_code} on {current_date.strftime('%Y-%m-%d')}. Falling back...")

        # 3. Pure Quantitative Fallback
        if self.pure_quant_fallback_enabled:
            logging.info(f"Pure quantitative fallback for {stock_code} on {current_date.strftime('%Y-%m-%d')}. No LLM decision available.")
            return False, "Pure quantitative decision: No LLM decision available."

        return True, "No valid decision path found, blocking buy."

# 示例用法 (在实际回测中，这些将由 backtest.py 传入)
if __name__ == "__main__":
    # 模拟初始化 Qdrant 和 Embedding 模型
    # 在实际应用中，这些应该在 KnowledgeBaseUpdater 中初始化并传递
    # 这里为了演示，直接初始化
    index_dir = "knowledge_base/index"
    embedding_model_name = "BAAI/bge-small-zh-v1.5"
    qdrant_client_test = QdrantClient(path=os.path.join(index_dir, "qdrant_db"))
    embedding_model_test = SentenceTransformer(embedding_model_name)

    # 确保 Qdrant collection 存在
    collection_name = "knowledge_base"
    if not qdrant_client_test.collection_exists(collection_name=collection_name):
        qdrant_client_test.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(size=embedding_model_test.get_sentence_embedding_dimension(), distance=models.Distance.COSINE),
        )
        logging.info(f"Qdrant collection '{collection_name}' created for test.")

    risk_gate = RiskGate(qdrant_client=qdrant_client_test, embedding_model=embedding_model_test)

    # 模拟 NotebookLM 成功决策
    notebooklm_success_decision = {"success": True, "allow_buy": True, "reason": "NotebookLM: Positive outlook.", "source": "notebooklm"}
    blocked, reason = risk_gate.buy_blocked_reason("000001.SZ", datetime(2023, 1, 1), notebooklm_success_decision)
    print(f"Stock 000001.SZ, NotebookLM success: Blocked={blocked}, Reason={reason}")

    # 模拟 NotebookLM 失败，降级到 Local RAG
    notebooklm_fail_decision = {"success": False, "reason": "NotebookLM: API error.", "source": "notebooklm"}
    blocked, reason = risk_gate.buy_blocked_reason("000002.SZ", datetime(2023, 1, 1), notebooklm_fail_decision)
    print(f"Stock 000002.SZ, NotebookLM fail, Local RAG fallback: Blocked={blocked}, Reason={reason}")

    # 模拟 NotebookLM 和 Local RAG 都失败，降级到纯量化
    # 需要确保 local RAG 返回空结果，这里通过模拟来实现
    # 实际测试时，可以清空 Qdrant 数据库或查询一个不存在的股票
    risk_gate.qdrant_client = None # 模拟 Qdrant 客户端不可用
    blocked, reason = risk_gate.buy_blocked_reason("000003.SZ", datetime(2023, 1, 1), notebooklm_fail_decision)
    print(f"Stock 000003.SZ, NotebookLM fail, Local RAG fail, Pure Quant fallback: Blocked={blocked}, Reason={reason}")

    # 模拟 NotebookLM 禁用，直接使用 Local RAG
    risk_gate.require_notebooklm_for_buy = False
    risk_gate.qdrant_client = qdrant_client_test # 恢复 Qdrant 客户端
    blocked, reason = risk_gate.buy_blocked_reason("000004.SZ", datetime(2023, 1, 1), None)
    print(f"Stock 000004.SZ, NotebookLM disabled, Local RAG: Blocked={blocked}, Reason={reason}")
