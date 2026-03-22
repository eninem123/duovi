import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer


SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf"}


@dataclass
class SearchHit:
    score: float
    source_path: str
    title: str
    text: str


class LocalKnowledgeBase:
    def __init__(self, config=None, base_dir="."):
        config = config or {}
        self.enabled = bool(config.get("enabled", True))
        self.base_dir = Path(base_dir).resolve()
        self.raw_dir = self.base_dir / config.get("raw_dir", "knowledge_base/raw")
        self.processed_dir = self.base_dir / config.get("processed_dir", "knowledge_base/processed")
        self.index_dir = self.base_dir / config.get("index_dir", "knowledge_base/index")
        self.model_name = config.get("model_name", "BAAI/bge-small-zh-v1.5")
        self.top_k = int(config.get("top_k", 4))
        self.chunk_size = int(config.get("chunk_size", 500))
        self.chunk_overlap = int(config.get("chunk_overlap", 80))
        self.min_similarity = float(config.get("min_similarity", 0.35))
        ts = config.get("two_stage") or {}
        self.two_stage_enabled = bool(ts.get("enabled", False))
        self.two_stage = {
            "coarse_top_k": int(ts.get("coarse_top_k", 16)),
            "coarse_min_similarity": float(ts.get("coarse_min_similarity", 0.28)),
            "refine_anchor_chunks": int(ts.get("refine_anchor_chunks", 3)),
            "refine_snippet_chars": int(ts.get("refine_snippet_chars", 150)),
            "blend_alpha": float(ts.get("blend_alpha", 0.45)),
            "final_min_similarity": float(ts.get("final_min_similarity", self.min_similarity)),
        }
        self.metadata_path = self.index_dir / "chunks.json"
        self.embedding_path = self.index_dir / "embeddings.npy"
        self._model = None
        self._ensure_directories()

    def _ensure_directories(self):
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

    def _load_model(self):
        if self._model is None:
            logging.info("加载本地知识库 embedding 模型: %s", self.model_name)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _iter_source_files(self):
        for path in sorted(self.raw_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                yield path

    def _read_text(self, path: Path):
        suffix = path.suffix.lower()
        if suffix in {".md", ".txt"}:
            return path.read_text(encoding="utf-8")
        if suffix == ".pdf":
            reader = PdfReader(str(path))
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
            return "\n".join(page for page in pages if page)
        return ""

    def _chunk_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        start = 0
        step = max(1, self.chunk_size - self.chunk_overlap)
        while start < len(text):
            chunk = text[start:start + self.chunk_size].strip()
            if chunk:
                chunks.append(chunk)
            start += step
        return chunks

    def _source_signature(self):
        signature = []
        for path in self._iter_source_files():
            stat = path.stat()
            signature.append(
                {
                    "path": str(path.relative_to(self.base_dir)).replace("\\", "/"),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }
            )
        return signature

    def _index_is_fresh(self):
        if not self.metadata_path.exists() or not self.embedding_path.exists():
            return False
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return payload.get("sources") == self._source_signature()

    def build_index(self, force=False):
        if not self.enabled:
            return {"success": False, "reason": "local_rag disabled"}
        if not force and self._index_is_fresh():
            return {"success": True, "reason": "index fresh"}

        chunks = []
        sources = self._source_signature()
        for path in self._iter_source_files():
            try:
                text = self._read_text(path)
            except Exception as exc:
                logging.warning("读取本地知识文件失败 %s: %s", path, exc)
                continue
            for idx, chunk in enumerate(self._chunk_text(text)):
                chunks.append(
                    {
                        "chunk_id": f"{path.stem}-{idx}",
                        "source_path": str(path.relative_to(self.base_dir)).replace("\\", "/"),
                        "title": path.stem,
                        "text": chunk,
                    }
                )

        if not chunks:
            self.metadata_path.write_text(
                json.dumps({"chunks": [], "sources": sources}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            np.save(self.embedding_path, np.empty((0, 0), dtype=np.float32))
            return {"success": False, "reason": "no local documents"}

        model = self._load_model()
        texts = [chunk["text"] for chunk in chunks]
        embeddings = model.encode(texts, normalize_embeddings=True)
        np.save(self.embedding_path, np.asarray(embeddings, dtype=np.float32))
        self.metadata_path.write_text(
            json.dumps({"chunks": chunks, "sources": sources}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"success": True, "reason": "rebuilt", "chunk_count": len(chunks)}

    def _load_index(self):
        self.build_index(force=False)
        if not self.metadata_path.exists() or not self.embedding_path.exists():
            return [], np.empty((0, 0), dtype=np.float32)
        payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        chunks = payload.get("chunks") or []
        embeddings = np.load(self.embedding_path)
        return chunks, embeddings

    def status(self):
        sources = list(self._iter_source_files())
        chunks, embeddings = self._load_index()
        return {
            "enabled": self.enabled,
            "raw_document_count": len(sources),
            "chunk_count": len(chunks),
            "index_ready": bool(chunks and embeddings.size > 0),
            "model_name": self.model_name,
            "raw_dir": str(self.raw_dir),
            "index_dir": str(self.index_dir),
        }

    def _cosine_scores(self, query_embedding, embeddings):
        if embeddings.size == 0:
            return np.array([], dtype=np.float32)
        return np.dot(embeddings, query_embedding)

    def _hits_from_indices(self, chunks, scores, indices, score_override=None, min_similarity=None):
        min_similarity = self.min_similarity if min_similarity is None else min_similarity
        hits = []
        for idx in indices:
            idx = int(idx)
            score = float(score_override[idx]) if score_override is not None else float(scores[idx])
            if math.isnan(score) or score < min_similarity:
                continue
            chunk = chunks[idx]
            hits.append(
                SearchHit(
                    score=score,
                    source_path=chunk["source_path"],
                    title=chunk["title"],
                    text=chunk["text"],
                )
            )
        return hits

    def _pack_search_result(self, hits, retrieval_meta=None):
        if not hits:
            out = {
                "success": False,
                "source": "local_rag",
                "reason": "no relevant local evidence",
                "results": [],
            }
            if retrieval_meta:
                out["retrieval"] = retrieval_meta
            return out

        summary = "；".join(hit.text[:90] for hit in hits[:2])
        evidence = [f"{hit.title}: {hit.text[:120]}" for hit in hits[:3]]
        confidence = min(0.9, max(0.2, hits[0].score))
        persona_notes = {
            "data_arch": f"本地知识库命中 {len(hits)} 条，最高相似度 {hits[0].score:.2f}。",
            "notebooklm": f"NotebookLM 不可用时已回退到本地知识库，核心来源为 {hits[0].source_path}。",
            "game_psych": "本地知识库以制度、策略和研究框架为主，情绪与博弈仍需结合实时资金流验证。",
            "trend": "本地知识库提供的是背景框架，最终执行仍需服从实时行情与纪律约束。",
        }
        out = {
            "success": True,
            "source": "local_rag",
            "summary": summary,
            "evidence": evidence,
            "confidence": round(float(confidence), 4),
            "results": [
                {
                    "score": round(hit.score, 4),
                    "source_path": hit.source_path,
                    "title": hit.title,
                    "text": hit.text,
                }
                for hit in hits
            ],
            "persona_notes": persona_notes,
        }
        if retrieval_meta:
            out["retrieval"] = retrieval_meta
        return out

    def _search_single_stage(self, query, top_k=None):
        chunks, embeddings = self._load_index()
        if not chunks or embeddings.size == 0:
            return None, None, None, chunks, embeddings

        model = self._load_model()
        query_embedding = model.encode([query], normalize_embeddings=True)[0]
        scores = self._cosine_scores(query_embedding, embeddings)
        limit = top_k or self.top_k
        ranked_indices = np.argsort(scores)[::-1][:limit]
        hits = self._hits_from_indices(chunks, scores, ranked_indices)
        return hits, scores, query_embedding, chunks, embeddings

    def _search_two_stage(self, query, top_k=None):
        chunks, embeddings = self._load_index()
        if not chunks or embeddings.size == 0:
            return self._pack_search_result([], {"mode": "two_stage", "reason": "empty_index"})

        cfg = self.two_stage
        model = self._load_model()
        qe = model.encode([query], normalize_embeddings=True)[0]
        scores = self._cosine_scores(qe, embeddings)
        ranked = np.argsort(scores)[::-1]

        coarse_min = float(cfg["coarse_min_similarity"])
        coarse_k = int(cfg["coarse_top_k"])
        coarse_idx = []
        for idx in ranked:
            idx = int(idx)
            s = float(scores[idx])
            if math.isnan(s) or s < coarse_min:
                continue
            coarse_idx.append(idx)
            if len(coarse_idx) >= coarse_k:
                break

        limit = top_k or self.top_k
        if len(coarse_idx) < max(limit, 3):
            coarse_idx = [int(i) for i in ranked[: max(coarse_k, limit)]]

        anchors_n = int(cfg["refine_anchor_chunks"])
        snip = int(cfg["refine_snippet_chars"])
        parts = [query]
        for idx in coarse_idx[:anchors_n]:
            parts.append((chunks[int(idx)]["text"] or "")[:snip])
        refine_text = "\n".join(p for p in parts if p)
        qe2 = model.encode([refine_text], normalize_embeddings=True)[0]

        alpha = float(cfg["blend_alpha"])
        beta = max(0.0, min(1.0, 1.0 - alpha))
        combined = {}
        for idx in coarse_idx:
            idx = int(idx)
            s1 = float(scores[idx])
            s2 = float(np.dot(embeddings[idx], qe2))
            combined[idx] = alpha * s1 + beta * s2

        sorted_idx = sorted(combined.keys(), key=lambda i: combined[i], reverse=True)
        final_min = float(cfg["final_min_similarity"])
        hits = self._hits_from_indices(
            chunks,
            scores,
            sorted_idx,
            score_override=combined,
            min_similarity=final_min,
        )
        hits = hits[:limit]

        if not hits:
            hits2, _, _, _, _ = self._search_single_stage(query, top_k=limit)
            return self._pack_search_result(
                hits2 or [],
                {
                    "mode": "two_stage_fallback_single",
                    "coarse_pool": len(coarse_idx),
                },
            )

        return self._pack_search_result(
            hits,
            {
                "mode": "two_stage",
                "coarse_pool": len(coarse_idx),
                "refine_preview_chars": min(len(refine_text), 200),
            },
        )

    def search(self, query, top_k=None):
        if not self.enabled:
            return {
                "success": False,
                "source": "local_rag",
                "reason": "local_rag disabled",
                "results": [],
            }

        chunks, embeddings = self._load_index()
        if not chunks or embeddings.size == 0:
            return {
                "success": False,
                "source": "local_rag",
                "reason": "local_rag has no indexed documents",
                "results": [],
            }

        if self.two_stage_enabled:
            return self._search_two_stage(query, top_k=top_k)

        hits, _, _, _, _ = self._search_single_stage(query, top_k=top_k)
        return self._pack_search_result(hits or [], {"mode": "single_stage"})


if __name__ == "__main__":
    kb = LocalKnowledgeBase(base_dir=Path(__file__).parent)
    result = kb.build_index(force=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
