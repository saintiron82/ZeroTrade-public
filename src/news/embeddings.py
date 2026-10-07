"""뉴스 임베딩 — 한국어 SBERT로 벡터화."""

import re
import struct
import logging
import os
import numpy as np

logger = logging.getLogger(__name__)

_model = None
MODEL_NAME = "jhgan/ko-sbert-nli"
EMBEDDING_DIM = 768


def _strip_html(text: str) -> str:
    """HTML 태그 제거."""
    return re.sub(r'<[^>]+>', '', text).strip()


def get_model():
    """Lazy-load 임베딩 모델 (첫 호출 ~3초, 이후 즉시)."""
    global _model
    if os.getenv("FF_NEWS_EMBEDDINGS", "1").strip().lower() in ("0", "false", "no", "off"):
        raise RuntimeError("뉴스 임베딩 비활성화 (FF_NEWS_EMBEDDINGS=0)")
    if _model is None:
        # HuggingFace 로더가 optional file HEAD/GET 로그를 INFO로 많이 남긴다.
        # 서비스 로그에서는 실제 실패만 보이도록 낮춘다.
        for noisy_logger in (
            "httpx",
            "huggingface_hub",
            "sentence_transformers",
            "transformers",
        ):
            logging.getLogger(noisy_logger).setLevel(logging.WARNING)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
        logger.info("임베딩 모델 로드: %s (%d차원)", MODEL_NAME, EMBEDDING_DIM)
    return _model


def embed_text(text: str) -> bytes:
    """텍스트 → 384-dim float32 BLOB (1536 bytes)."""
    model = get_model()
    clean = _strip_html(text)
    vec = model.encode(clean, normalize_embeddings=True)
    return struct.pack(f'{EMBEDDING_DIM}f', *vec.tolist())


def embed_texts(texts: list[str]) -> list[bytes]:
    """배치 임베딩."""
    model = get_model()
    clean = [_strip_html(t) for t in texts]
    vecs = model.encode(clean, normalize_embeddings=True, batch_size=32)
    return [struct.pack(f'{EMBEDDING_DIM}f', *v.tolist()) for v in vecs]


def blob_to_array(blob: bytes) -> np.ndarray:
    """BLOB → numpy array."""
    return np.array(struct.unpack(f'{EMBEDDING_DIM}f', blob), dtype=np.float32)


def cosine_similarity(a: bytes, b: bytes) -> float:
    """두 BLOB 벡터의 코사인 유사도 (정규화 벡터이므로 dot product)."""
    va, vb = blob_to_array(a), blob_to_array(b)
    return float(np.dot(va, vb))
