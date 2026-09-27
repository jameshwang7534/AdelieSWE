"""BM25 with positive smoothed IDF, term saturation and document-length normalization."""

import math
from collections import Counter
from uuid import UUID

from app.retrieval.base import ChunkSource
from app.retrieval.tokenization import tokenize
from app.schemas.search import SearchRequest, SearchResult


class BM25Retriever:
    def __init__(self, source: ChunkSource) -> None:
        self.source = source

    def search(self, repository_id: UUID, query: str, top_k: int = 10) -> list[SearchResult]:
        request = SearchRequest(query=query, top_k=top_k)
        documents = self.source.load(repository_id)
        terms = sorted(set(tokenize(request.query)))
        if not documents or not terms:
            return []
        frequencies = [Counter(tokenize(doc.file_path + "\n" + doc.content)) for doc in documents]
        lengths = [sum(frequency.values()) for frequency in frequencies]
        average = sum(lengths) / len(documents)
        if average == 0:
            return []
        document_frequency: Counter[str] = Counter()
        for frequency in frequencies:
            document_frequency.update(frequency.keys())
        k1, b = 1.5, 0.75
        scores: list[tuple[float, int]] = []
        for index, frequency in enumerate(frequencies):
            score = 0.0
            normalization = k1 * (1 - b + b * lengths[index] / average)
            for term in terms:
                count = frequency[term]
                if count:
                    df = document_frequency[term]
                    idf = math.log1p((len(documents) - df + 0.5) / (df + 0.5))
                    score += idf * count * (k1 + 1) / (count + normalization)
            if score > 0:
                scores.append((score, index))
        scores.sort(
            key=lambda item: (
                -item[0],
                documents[item[1]].file_path,
                documents[item[1]].start_line,
                documents[item[1]].end_line,
                str(documents[item[1]].id),
            )
        )
        return [
            SearchResult(
                chunk_id=documents[index].id,
                file_path=documents[index].file_path,
                start_line=documents[index].start_line,
                end_line=documents[index].end_line,
                content=documents[index].content,
                score=score,
                rank=rank,
            )
            for rank, (score, index) in enumerate(scores[: request.top_k], start=1)
        ]
