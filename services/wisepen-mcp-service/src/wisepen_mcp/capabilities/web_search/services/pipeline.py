from __future__ import annotations

from zeroentropy import AsyncZeroEntropy

from common.utils.ranking import (
    RankCandidate,
    RankQuery,
    RankRequest,
    RankingPipeline,
)
from common.utils.ranking.fusion import WeightedRrfFusion
from common.utils.ranking.rerankers import ZeroEntropyReranker, ZeroEntropyRerankerConfig
from common.utils.ranking.scorers import FieldedBM25Scorer, FieldedBM25ScorerConfig
from common.utils.ranking.tokenizer import ThuLacRankingTokenizer

from .models import (
    SearchMode,
    SearchPipelineResult,
    SearchResponse,
    WebSearchCandidate,
)
from .providers.base import ProviderSearcher

def build_web_search_ranking_pipeline() -> RankingPipeline:
    from wisepen_mcp.core.config.app_settings import settings

    reranker = None
    if settings.ZERO_ENTROPY_API_KEY:
        reranker = ZeroEntropyReranker(
            client=AsyncZeroEntropy(api_key=settings.ZERO_ENTROPY_API_KEY),
            config=ZeroEntropyRerankerConfig(model=settings.RERANKER_MODEL),
        )

    return RankingPipeline(
        scorers=(
            FieldedBM25Scorer(
                tokenizer=ThuLacRankingTokenizer(),
                config=FieldedBM25ScorerConfig(
                    field_weights={
                        "title": 3.0,
                        "snippet": 1.5,
                        "highlights": 1.0,
                    },
                    min_score=-1.0,
                ),
            ),
        ),
        fusion=WeightedRrfFusion(),
        reranker=reranker,
    )

class SearchPipeline:
    """执行搜索并按字段相关性和问句重排候选。"""

    def __init__(self, *, ranking_pipeline: RankingPipeline) -> None:
        self._ranking_pipeline = build_web_search_ranking_pipeline()

    async def search(
        self,
        *,
        search_query: str,
        ranking_query: str,
        max_results: int,
        searcher: ProviderSearcher,
        mode: SearchMode,
    ) -> SearchPipelineResult:
        response = await self._request_provider_response(
            query=search_query,
            max_results=max_results,
            searcher=searcher,
            mode=mode,
        )

        candidates = tuple(
            WebSearchCandidate(
                candidate_id=f"[{index}]",
                title=item.title,
                url=item.url,
                snippet=item.snippet,
                highlights=item.highlights,
            )
            for index, item in enumerate(response.results, 1)
        )

        ranked = await self._ranking_pipeline.arank(
            RankRequest(
                query=RankQuery(text=ranking_query),
                candidates=tuple(
                    RankCandidate(
                        candidate_id=candidate.candidate_id,
                        text="\n".join(
                            text
                            for text in (
                                (
                                    f"Title: {candidate.title}"
                                    if candidate.title
                                    else ""
                                ),
                                (
                                    f"Snippet: {candidate.snippet}"
                                    if candidate.snippet
                                    else ""
                                ),
                                *(
                                    f"Highlight: {highlight}"
                                    for highlight in candidate.highlights or ()
                                ),
                            )
                            if text
                        ),
                        fields={
                            "title": candidate.title or "",
                            "snippet": candidate.snippet or "",
                            "highlights": "\n".join(candidate.highlights or ()),
                        },
                    )
                    for candidate in candidates
                ),
                top_k=len(candidates),
                candidate_limit=len(candidates),
            )
        )
        candidates_by_id = {
            candidate.candidate_id: candidate for candidate in candidates
        }

        return SearchPipelineResult(
            search_query=search_query,
            response=response,
            candidates=tuple(
                candidates_by_id[item.candidate_id] for item in ranked.ranked
            ),
        )

    async def _request_provider_response(
        self,
        *,
        query: str,
        max_results: int,
        searcher: ProviderSearcher,
        mode: SearchMode,
    ) -> SearchResponse:
        search = (
            searcher.search_academic
            if mode is SearchMode.ACADEMIC
            else searcher.search_web
        )

        return await search(
            query=query,
            max_results=max_results,
        )
