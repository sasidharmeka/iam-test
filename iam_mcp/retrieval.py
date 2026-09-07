"""Search the whole trusted catalog before asking an LLM to classify a question."""

from __future__ import annotations

import math
import re
from difflib import get_close_matches

from iam_agent.models import (
    AnalysisPlan,
    CatalogRetrieval,
    ConversationTurn,
    EvidenceRequirement,
    FilterOperator,
    QueryOperation,
)
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.validator import QueryPlanError

STOP_WORDS = set(
    "a an and are as at be by can do for from give how i in is it me of on or our please "
    "return show tell that the their them these this to users user was what which who with "
    "you your report export csv excel pdf word xlsx docx".split()
)
FOLLOWUP = re.compile(r"\b(their|them|those|same|this analysis|that analysis|what about)\b", re.I)
IDENTITY = re.compile(r"\b(?:user[ _-]*\d+|u\d+)\b", re.I)


class CatalogRetriever:
    """Deterministic lexical/alias retrieval with typo tolerance and broad fallback.

    Scores rank candidates; they never grant permission. Strong catalog topic matches become
    explicit evidence requirements. When matching is inconclusive, every concept stays available
    so a hard top-one domain gate cannot silently exclude the right schema.
    """

    def __init__(self, catalog: SemanticCatalog):
        self.catalog = catalog
        self.documents = {
            name: self._tokens(
                " ".join(
                    [name, concept.description, concept.entity, concept.grain]
                    + concept.synonyms
                    + list(concept.fields)
                    + list(concept.metrics)
                )
            )
            for name, concept in catalog.concepts.items()
        }
        self.vocabulary = set().union(*self.documents.values())
        for topic in catalog.routing_topics.values():
            for alias in topic.aliases:
                self.vocabulary.update(self._tokens(alias))

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.casefold())) - STOP_WORDS

    def _normalize(self, text: str) -> str:
        words = re.findall(r"[a-z0-9]+", text.casefold())
        normalized = []
        for word in words:
            if word not in self.vocabulary and word not in STOP_WORDS and word.isalpha():
                # Never fuzzy-match identifiers, numbers, filter values, or short acronyms.
                alternatives = (
                    get_close_matches(word, sorted(self.vocabulary), n=1, cutoff=0.85)
                    if len(word) >= 5
                    else []
                )
                word = alternatives[0] if alternatives else word
            normalized.append(word)
        return " ".join(normalized)

    def retrieve(
        self, question: str, history: list[ConversationTurn] | None = None
    ) -> CatalogRetrieval:
        history = history or []
        entities = list(
            dict.fromkeys(
                re.sub(r"[ _-]", "", match.casefold()) for match in IDENTITY.findall(question)
            )
        )
        search_question = question
        if FOLLOWUP.search(question) and history:
            previous = history[-1]
            current = self._normalize(question)
            has_current_topic = any(
                f" {alias.casefold()} " in f" {current} "
                for topic in self.catalog.routing_topics.values()
                for alias in topic.aliases
            )
            if not has_current_topic:
                search_question += " " + previous.user_message
            if not entities:
                entities = list(previous.entities)
        normalized = self._normalize(search_question)
        tokens = self._tokens(normalized)
        scores: dict[str, float] = {}
        for name, words in self.documents.items():
            score = 0.0
            for token in tokens & words:
                frequency = sum(token in other for other in self.documents.values())
                score += math.log(1 + len(self.documents) / frequency)
            scores[name] = score

        requirements: list[EvidenceRequirement] = []
        reasons: list[str] = []
        for name, topic in self.catalog.routing_topics.items():
            aliases = [
                re.sub(r"[^a-z0-9]+", " ", alias.casefold()).strip() for alias in topic.aliases
            ]
            if any(f" {alias} " in f" {normalized} " for alias in aliases):
                requirements.append(EvidenceRequirement(topic=name, concepts=topic.concepts))
                reasons.append(f"Matched catalog topic: {name}")
                for concept in topic.concepts:
                    scores[concept] += 10

        # Exact concept requests are stronger than a broad domain label.
        for name in self.catalog.concepts:
            if f" {name.replace('_', ' ')} " in f" {normalized} ":
                requirements.append(EvidenceRequirement(topic=name, concepts=[name]))
                scores[name] += 20

        broad_profile = bool(entities) and bool(
            re.search(r"\b(tell me about|profile|overview|everything|history)\b", normalized)
        )
        if broad_profile:
            for name in ("risk_scores", "identity_timelines"):
                requirements.append(EvidenceRequirement(topic=name, concepts=[name]))
                scores[name] += 20
            reasons.append("Identity profile requires both summary and chronological evidence")
        if any(item.topic == "risk" for item in requirements) and re.search(r"\bwhy\b", normalized):
            requirements.append(
                EvidenceRequirement(
                    topic="risk_reasons", concepts=["risk_scores"], required_fields=["risk_factors"]
                )
            )

        # Keep narrower requirements when a broad one would be satisfied by the same evidence.
        distinct: dict[tuple, EvidenceRequirement] = {}
        for item in requirements:
            key = (tuple(sorted(item.concepts)), tuple(item.required_fields))
            distinct[key] = item
        requirements = [
            item
            for item in distinct.values()
            if not any(
                set(other.concepts) < set(item.concepts) and not item.required_fields
                for other in distinct.values()
            )
        ]
        general = bool(
            re.match(r"\s*(what (?:is|does)|explain|define|hello|hi\b|hey\b)", question, re.I)
        )
        project_cues = bool(
            re.search(
                r"\b(our|my|this project|in the data|which|who|show|list|compare|count|rank)\b",
                question,
                re.I,
            )
        )
        requires_data = bool(entities) or (bool(requirements) and (not general or project_cues))
        if not requires_data:
            requirements = []

        ranked = sorted(scores, key=lambda name: (-scores[name], name))
        low_confidence = not reasons
        # Broad or ambiguous questions use the complete small catalog. For specific questions,
        # union the top candidates with every explicitly matched topic; never select one domain.
        candidates = (
            ranked
            if low_confidence or broad_profile
            else [name for name in ranked if scores[name] > 0][:8]
        )
        candidates = list(
            dict.fromkeys(candidates + [c for item in requirements for c in item.concepts])
        )
        domains = [
            domain
            for domain in self.catalog.domains
            if any(domain in self.catalog.concept(name).domains for name in candidates)
        ]
        return CatalogRetrieval(
            catalog_version=self.catalog.version,
            candidate_concepts=candidates,
            candidate_domains=domains,
            requirements=requirements,
            entities=entities,
            requires_project_data=requires_data,
            low_confidence=low_confidence,
            reasons=reasons or ["Low retrieval confidence: keep the entire catalog available"],
            catalog_context=self.catalog.planner_context(domains, concept_names=candidates),
        )


class EvidenceCoverageValidator:
    """Check question coverage in addition to SQL safety and catalog membership."""

    def __init__(self, catalog: SemanticCatalog):
        self.catalog = catalog

    def validate(self, plan: AnalysisPlan, retrieval: CatalogRetrieval) -> None:
        for requirement in retrieval.requirements:
            covering = [step for step in plan.steps if step.concept in requirement.concepts]
            if requirement.required_fields:
                covering = [
                    step
                    for step in covering
                    if step.operation == QueryOperation.RECORDS
                    and set(requirement.required_fields).issubset(
                        step.fields or self.catalog.concept(step.concept).default_fields
                    )
                ]
            if not covering:
                raise QueryPlanError(
                    f"Missing evidence for topic {requirement.topic!r}; use one of "
                    f"{requirement.concepts}, "
                    f"required record fields {requirement.required_fields}. "
                    "A valid table in another domain does not satisfy this topic."
                )
        for step in plan.steps:
            concept = self.catalog.concept(step.concept)
            if retrieval.entities and not concept.user_field:
                raise QueryPlanError(
                    "An identity-specific question cannot use unscoped global data"
                )
            if not retrieval.entities:
                continue
            constraints = [
                item
                for item in step.filters
                if item.field == concept.user_field
                and item.operator in {FilterOperator.EQUALS, FilterOperator.IN}
            ]
            if not constraints or not any(
                set(
                    str(value).casefold()
                    for value in (item.value if isinstance(item.value, list) else [item.value])
                )
                == set(retrieval.entities)
                for item in constraints
            ):
                raise QueryPlanError(
                    f"Step {step.step_id!r} must filter {concept.user_field!r} to exactly "
                    f"the requested identities {retrieval.entities}"
                )
