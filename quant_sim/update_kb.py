import yaml
import os
import logging
from datetime import datetime, timedelta
import akshare as ak
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient, models

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KnowledgeBaseUpdater:
    def __init__(self, config_path="config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        self.local_rag_cfg = self.config.get("local_rag", {})
        self.kb_update_cfg = self.config.get("knowledge_base_update", {})
        
        self.raw_dir = self.local_rag_cfg.get("raw_dir", "knowledge_base/raw")
        self.processed_dir = self.local_rag_cfg.get("processed_dir", "knowledge_base/processed")
        self.index_dir = self.local_rag_cfg.get("index_dir", "knowledge_base/index")
        self.embedding_model_name = self.kb_update_cfg.get("embedding_model", "BAAI/bge-small-zh-v1.5")
        
        # 确保目录存在
        os.makedirs(self.raw_dir, exist_ok=True)
        os.makedirs(self.processed_dir, exist_ok=True)
        os.makedirs(self.index_dir, exist_ok=True)
        
        self.model = SentenceTransformer(self.embedding_model_name)
        self.qdrant_client = QdrantClient(path=os.path.join(self.index_dir, "qdrant_db"))
        self.collection_name = "knowledge_base"
        
        # 检查并创建 Qdrant collection
        if not self.qdrant_client.collection_exists(collection_name=self.collection_name):
            self.qdrant_client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(size=self.model.get_sentence_embedding_dimension(), distance=models.Distance.COSINE),
            )
            logging.info(f"Qdrant collection '{self.collection_name}' created.")

    def _fetch_akshare_news(self, start_date: str, end_date: str):
        logging.info(f"Fetching AKShare news from {start_date} to {end_date}...")
        try:
            # ak.stock_news_em(start_date=start_date, end_date=end_date)
            # 示例：这里需要根据实际 akshare API 调整，目前没有直接的按日期范围获取新闻的 API
            # 暂时模拟数据
            news_data = [
                {"date": "2024-03-27", "title": "某公司发布利好公告", "content": "详细内容..."},
                {"date": "2024-03-26", "title": "行业政策出台", "content": "详细内容..."},
            ]
            return news_data
        except Exception as e:
            logging.error(f"Failed to fetch AKShare news: {e}")
            return []

    def _fetch_tushare_research(self, start_date: str, end_date: str):
        logging.info(f"Fetching Tushare research reports from {start_date} to {end_date}...")
        try:
            # Tushare 需要 token，这里仅作示例
            # import tushare as ts
            # pro = ts.pro_api('YOUR_TUSHARE_TOKEN')
            # df = pro.research_report(start_date=start_date, end_date=end_date)
            # 暂时模拟数据
            research_data = [
                {"date": "2024-03-27", "title": "某券商研报：看好某行业", "content": "详细分析..."},
                {"date": "2024-03-25", "title": "公司深度报告", "content": "详细分析..."},
            ]
            return research_data
        except Exception as e:
            logging.error(f"Failed to fetch Tushare research: {e}")
            return []

    def _process_document(self, doc_id: str, content: str):
        # 简单的文本分块，实际应用中可能需要更复杂的策略
        chunk_size = self.local_rag_cfg.get("chunk_size", 500)
        chunk_overlap = self.local_rag_cfg.get("chunk_overlap", 80)
        
        chunks = []
        for i in range(0, len(content), chunk_size - chunk_overlap):
            chunk = content[i : i + chunk_size]
            chunks.append(chunk)
        return chunks

    def _generate_embeddings(self, texts: list[str]):
        return self.model.encode(texts, show_progress_bar=False).tolist()

    def update_knowledge_base(self):
        if not self.kb_update_cfg.get("enabled", False):
            logging.info("Knowledge base update is disabled in config.")
            return

        logging.info("Starting knowledge base update...")
        today = datetime.now()
        # 假设我们每天更新前一天的内容
        yesterday = today - timedelta(days=1)
        start_date = yesterday.strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")

        all_documents = []
        if "akshare_news" in self.kb_update_cfg.get("sources", []):
            all_documents.extend(self._fetch_akshare_news(start_date, end_date))
        if "tushare_research" in self.kb_update_cfg.get("sources", []):
            all_documents.extend(self._fetch_tushare_research(start_date, end_date))

        if not all_documents:
            logging.info("No new documents fetched.")
            return

        points = []
        for i, doc in enumerate(all_documents):
            doc_id = f"{doc['date']}_{doc['title']}_{i}"
            chunks = self._process_document(doc_id, doc['content'])
            embeddings = self._generate_embeddings(chunks)
            
            import uuid
            for j, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                points.append(
                    models.PointStruct(
                        id=str(uuid.uuid4()), # 使用 UUID 作为 Point ID
                        vector=embedding,
                        payload={
                            "original_doc_id": doc_id,
                            "title": doc['title'],
                            "date": doc['date'],
                            "content": chunk,
                            "source": "akshare_news" if "akshare_news" in self.kb_update_cfg.get("sources", []) else "tushare_research"
                        },
                    )
                )
        
        if points:
            self.qdrant_client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True
            )
            logging.info(f"Successfully updated Qdrant with {len(points)} new points.")
        else:
            logging.info("No points to upsert to Qdrant.")

        logging.info("Knowledge base update finished.")

if __name__ == "__main__":
    updater = KnowledgeBaseUpdater()
    updater.update_knowledge_base()
