"""把演示语料写入本地知识库（仅当索引为空）。

用途：让前端「深度检索」在没有真实文档时也能演示带引用的回答。
语料是虚构公司制度（60 chunk / 20 文档，含冲突/缺口/近义条款），全部标为公开。

安全约束：
- 只在 `vector_store.count() == 0` 时写入，避免把演示数据混进真实知识库；
- 不删除任何已有数据。要回到空库，删除 `data/chroma_db` 后重启即可。
"""

import asyncio
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.models.chat import DocumentChunk
from app.services.vector_store import vector_store
from evaluation.agent_corpus import AGENT_CORPUS


async def main() -> int:
    await vector_store.initialize()
    existing = await vector_store.count()
    if existing:
        print(f"索引已有 {existing} 个 chunk，拒绝写入演示数据（避免混入真实知识库）。")
        return 1

    chunks = [
        DocumentChunk(
            chunk_id=item["chunk_id"],
            doc_id=item["doc_id"],
            content=item["content"],
            metadata={
                "document_id": item["doc_id"],
                "title": item["title"],
                "filename": item["title"],
                "chunk_index": index,
                # 演示数据全部公开，否则非管理员账号检索不到
                "owner": "demo",
                "is_public": True,
            },
        )
        for index, item in enumerate(AGENT_CORPUS)
    ]
    await vector_store.add_chunks(chunks)

    from app.main import _rebuild_retrieval_indexes

    await _rebuild_retrieval_indexes()
    print(f"已写入演示语料：{len(chunks)} chunk / {len({c['doc_id'] for c in AGENT_CORPUS})} 文档")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
