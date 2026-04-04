import yaml
import os
import logging
from datetime import datetime, timedelta
import akshare as ak
import tushare as ts
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient, models
import uuid

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KnowledgeBaseUpdater:
    def __init__(self, config_path="config.yaml", qdrant_client=None, embedding_model=None):
        script_dir = os.path.dirname(__file__)
        config_full_path = os.path.join(script_dir, config_path)
        with open(config_full_path, "r", encoding="utf-8") as f:
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
        
        # 直接使用传入的实例，不进行重新初始化
        if qdrant_client is None or embedding_model is None:
            raise ValueError("QdrantClient and embedding_model must be provided to KnowledgeBaseUpdater")

        self.model = embedding_model
        self.qdrant_client = qdrant_client
        self.collection_name = "knowledge_base"
        
        # 检查并创建 Qdrant collection
        if not self.qdrant_client.collection_exists(collection_name=self.collection_name):
            self.qdrant_client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(size=self.model.get_sentence_embedding_dimension(), distance=models.Distance.COSINE),
            )
            logging.info(f"Qdrant collection '{self.collection_name}' created.")

    def _fetch_akshare_news(self):
        logging.info(f"Fetching AKShare news (using news_cctv as general news source)...")
        try:
            news_df = ak.news_cctv(date=datetime.now().strftime("%Y%m%d"))
            news_data = []
            for _, row in news_df.iterrows():
                news_data.append({"date": row["date"], "title": row["title"], "content": row["content"]})
            return news_data
        except Exception as e:
            logging.error(f"Failed to fetch AKShare news: {e}")
            return []

    def _fetch_tushare_research(self, start_date: str, end_date: str):
        logging.info(f"Fetching Tushare research reports from {start_date} to {end_date}...")
        try:
            tushare_token = os.environ.get("TUSHARE_TOKEN") or self.kb_update_cfg.get("tushare_token")
            if not tushare_token:
                logging.warning("Tushare token not found. Skipping Tushare research fetching.")
                return []
            pro = ts.pro_api(tushare_token)
            df = pro.news(start_date=start_date, end_date=end_date) # 暂时用 news接口代替，实际应为研报接口
            research_data = []
            for _, row in df.iterrows():
                research_data.append({"date": row["trade_date"], "title": row["title"], "content": row["content"]})
            return research_data
        except Exception as e:
            logging.error(f"Failed to fetch Tushare research: {e}")
            return []

    def _process_document(self, doc_id: str, content: str):
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
            return self.qdrant_client, self.model

        logging.info("Starting knowledge base update...")
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        
        all_documents = []
        if "akshare_news" in self.kb_update_cfg.get("sources", []):
            all_documents.extend(self._fetch_akshare_news())
        if "tushare_research" in self.kb_update_cfg.get("sources", []):
            all_documents.extend(self._fetch_tushare_research(yesterday.strftime("%Y%m%d"), today.strftime("%Y%m%d")))

        if not all_documents:
            logging.info("No new documents fetched.")
            return self.qdrant_client, self.model

        points = []
        for i, doc in enumerate(all_documents):
            doc_id = f"{doc['date']}_{doc['title']}_{i}"
            chunks = self._process_document(doc_id, doc['content'])
            embeddings = self._generate_embeddings(chunks)
            
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
        return self.qdrant_client, self.model

def main():
    # 在这里初始化 QdrantClient 和 SentenceTransformer，并传递给 KnowledgeBaseUpdater
    config_path = "config.yaml"
    script_dir = os.path.dirname(__file__)
    config_full_path = os.path.join(script_dir, config_path)
    with open(config_full_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    local_rag_cfg = config.get("local_rag", {})
    kb_update_cfg = config.get("knowledge_base_update", {})
    index_dir = local_rag_cfg.get("index_dir", "knowledge_base/index")
    embedding_model_name = kb_update_cfg.get("embedding_model", "BAAI/bge-small-zh-v1.5")

    qdrant_client = QdrantClient(path=os.path.join(index_dir, "qdrant_db"))
    embedding_model = SentenceTransformer(embedding_model_name)

    # 不再在这里调用 update_knowledge_base，只返回初始化好的客户端和模型
    return qdrant_client, embedding_model

if __name__ == "__main__":
    # 仅用于测试 update_kb.py 自身的功能，实际回测时由 backtest.py 调用 main() 并传递实例
    qdrant_client, embedding_model = main()
    updater = KnowledgeBaseUpdater(qdrant_client=qdrant_client, embedding_model=embedding_model)
    updater.update_knowledge_base()
