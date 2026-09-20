"""Entity resolution — the pipeline stage that turns extraction output into a graph.

The extraction model reads one chunk at a time and remembers nothing between
chunks, so it emits the same thing under several surface forms:

    cybercrime  /  Cybercrime  /  CYBER CRIME        -> 3 nodes
    PC  /  PCs                                       -> 2 nodes
    IPv6  /  IPv6 protocol                           -> 2 nodes

No extraction model fixes this; it is structural. Reconciling those into one
"golden record" per real-world thing is its own stage, and in practice it is the
stage that decides whether a graph is usable — extraction is the cheap part.

## The method: blocking, then pairwise matching

Classic record linkage. Comparing every entity with every other is O(n^2), so
first **block**: only compare entities that could plausibly match. We block on
`type`, because a `person` is never the same node as a `technology` no matter how
similar the strings are. Within a block, score pairs and merge above a threshold.

String similarity is the first-pass matcher. It catches case and morphology
(`Cybercrime` / `CYBER CRIME`, `PC` / `PCs`) but not synonymy — `PC` and
`computer` share no characters and will stay separate until this is upgraded to
embeddings. That upgrade slots in at `_similar_pairs` without touching anything
else.

## What is deliberately left alone

Nodes the user has confirmed (`user_edited`) are never auto-merged. The user
asserted those; silently folding one into another would undo a human decision.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

from rapidfuzz import fuzz, process

from graphforge.ai import adjudicate
from graphforge.core.config import get_limits
from graphforge.extraction.schema import normalize
from graphforge.graph import client, embeddings

log = logging.getLogger(__name__)

# rapidfuzz ratio (0-100) above which two names in the same block are the same
# thing. 88 merges "Cybercrime"/"CYBER CRIME" and "IPv6"/"IPv6 protocol" while
# leaving "IPv4"/"IPv6" apart -- those differ by one character but are opposites,
# which is exactly the failure mode a lower threshold would cause.
DEFAULT_THRESHOLD = 88.0

# Below this length, fuzzy ratios are unreliable: "AI" vs "API" scores high but
# they are different things. Short names must match exactly (after normalising).
MIN_FUZZY_LENGTH = 5


def _acronym_of(phrase: str) -> str:
    """Initials of a multi-word phrase: "personal computer" -> "pc"."""
    words = [w for w in normalize(phrase).split() if w]
    return "".join(w[0] for w in words) if len(words) > 1 else ""


def _is_acronym_pair(a: str, b: str) -> bool:
    """True when one name looks like the initials of the other.

    Abbreviations are the case embeddings handle worst. Measured: "PC" scores
    only 0.59 against "personal computer", and the plural "PCs" drops it to
    0.52 -- under any threshold that is not also full of noise. A 384-dimension
    vector of two isolated tokens simply does not encode "these letters stand
    for those words".

    Initials do, deterministically and for free. This only nominates a pair as
    a *candidate*; the language model still decides, because initials are
    ambiguous ("US" is United States or user story depending on the document).
    """
    for short, long in ((a, b), (b, a)):
        letters = normalize(short).replace(" ", "")
        initials = _acronym_of(long)
        if not initials:
            continue
        # Both forms: "PCs" is a plural of "PC", but the S in "AWS" is a real
        # initial. Stripping unconditionally breaks the second case.
        for candidate in (letters, letters.rstrip("s")):
            if 2 <= len(candidate) <= 6 and candidate == initials:
                return True
    return False


class _Union:
    """Union-find, so A~B and B~C put all three in one group."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for node in self.parent:
            out[self.find(node)].append(node)
        return out


async def resolve_workspace(
    workspace_id: uuid.UUID, threshold: float = DEFAULT_THRESHOLD
) -> dict:
    """Merge duplicate entities across a whole workspace.

    Runs after ingestion, over the entire workspace rather than one document,
    because the duplicates that matter most are the ones spanning documents --
    that is what turns several per-document subgraphs into one usable graph.
    """
    entities = await client.run(
        """
        MATCH (e:Entity {workspace_id: $workspace_id})
        WHERE coalesce(e.user_edited, false) = false
        OPTIONAL MATCH (e)-[r:REL]-()
        RETURN e.id AS id, e.name AS name, e.type AS type,
               e.canonical_name AS canonical, count(r) AS degree
        """,
        workspace_id,
    )
    if len(entities) < 2:
        return {"examined": len(entities), "groups": 0, "merged": 0}

    # --- block by type ---------------------------------------------------
    blocks: dict[str, list[dict]] = defaultdict(list)
    for e in entities:
        blocks[e["type"] or "concept"].append(e)

    union = _Union()
    for block in blocks.values():
        for a_id, b_id in _similar_pairs(block, threshold):
            union.union(a_id, b_id)

    # --- merge each group ------------------------------------------------
    by_id = {e["id"]: e for e in entities}
    merged_total = 0
    groups = 0

    for members in union.groups().values():
        if len(members) < 2:
            continue
        groups += 1

        # Survivor = most connected, tie-broken by longest name. The most
        # connected node is the one the graph already treats as central, so
        # merging into it preserves the most structure.
        ordered = sorted(
            members,
            key=lambda i: (by_id[i]["degree"], len(by_id[i]["name"] or "")),
            reverse=True,
        )
        survivor, duplicates = ordered[0], ordered[1:]

        try:
            await _merge_into(workspace_id, survivor, duplicates)
            merged_total += len(duplicates)
            log.info(
                "resolved %s <- %s",
                by_id[survivor]["name"],
                [by_id[d]["name"] for d in duplicates],
            )
        except Exception:
            # One bad group must not abort the rest of the pass.
            log.exception("failed to merge group around %s", survivor)

    return {"examined": len(entities), "groups": groups, "merged": merged_total}


def _similar_pairs(block: list[dict], threshold: float) -> list[tuple[str, str]]:
    """Pairs within one block that are the same thing.

    Three tiers, cheapest first, because the expensive one cannot run on
    everything -- 500 entities is 125,000 pairs:

        1. string similarity      free       decides on its own
        2. embedding cosine       ~1 s/500   decides only when very high
        3. a language model       ~1 call    decides the ambiguous middle

    Tier 2 exists to *shortlist* for tier 3, not to decide. Measured on a real
    workspace it scored `GraphForge` against `Graph Databases` at 0.65 -- a
    product and a category. Only scores at or above `embed_merge` are trusted
    outright; the band below goes to the model.
    """
    names = [e["canonical"] or normalize(e["name"] or "") for e in block]
    display = [e["name"] or "" for e in block]
    ids = [e["id"] for e in block]

    pairs: list[tuple[str, str]] = []
    undecided: list[tuple[int, int]] = []

    # --- tier 1: string similarity ---------------------------------------
    scores = process.cdist(names, names, scorer=fuzz.ratio, workers=-1)
    matched: set[tuple[int, int]] = set()

    for i in range(len(block)):
        for j in range(i + 1, len(block)):
            a, b = names[i], names[j]
            if not a or not b:
                continue
            if len(a) < MIN_FUZZY_LENGTH or len(b) < MIN_FUZZY_LENGTH:
                # Short strings: exact match only. "AI" vs "API" scores 80.
                if a == b:
                    pairs.append((ids[i], ids[j]))
                    matched.add((i, j))
                continue
            if scores[i][j] >= threshold:
                pairs.append((ids[i], ids[j]))
                matched.add((i, j))

    limits = get_limits()
    if not limits.resolution_use_embeddings or len(block) < 2:
        return pairs

    # --- tier 2: embeddings ----------------------------------------------
    try:
        cosine = embeddings.similarity_matrix(display)
    except Exception:
        log.exception("embedding step failed; falling back to string matching only")
        return pairs

    scored: list[tuple[float, int, int]] = []
    for i in range(len(block)):
        for j in range(i + 1, len(block)):
            if (i, j) in matched:
                continue
            score = float(cosine[i][j])
            if score >= limits.resolution_embed_merge:
                pairs.append((ids[i], ids[j]))
            elif score >= limits.resolution_embed_ask:
                scored.append((score, i, j))
            elif _is_acronym_pair(display[i], display[j]):
                # Below the cosine floor but structurally suspicious. Ranked
                # just under the genuine near-misses so the budget cap, if it
                # bites, spends on those first.
                scored.append((limits.resolution_embed_ask - 0.01, i, j))

    # Highest similarity first, so when the budget cap bites it spends what is
    # left on the most likely duplicates rather than an arbitrary slice.
    scored.sort(reverse=True)
    undecided = [(i, j) for _, i, j in scored]

    # --- tier 3: ask the model about the middle band ---------------------
    if undecided and limits.resolution_use_llm:
        # Cap it: a pathological workspace should not turn into a huge bill.
        undecided = undecided[: limits.resolution_max_llm_pairs]
        try:
            verdicts = adjudicate.adjudicate([(display[i], display[j]) for i, j in undecided])
        except Exception:
            log.exception("adjudication failed; leaving ambiguous pairs unmerged")
            verdicts = [False] * len(undecided)

        for (i, j), same in zip(undecided, verdicts):
            if same:
                pairs.append((ids[i], ids[j]))

    return pairs


async def _merge_into(
    workspace_id: uuid.UUID, survivor_id: str, duplicate_ids: list[str]
) -> None:
    """Fold duplicates into the survivor, keeping every relationship.

    `apoc.refactor.mergeNodes` merges into the *first* node given, so the
    survivor leads the list. `mergeRels: true` collapses relationships that
    become parallel after the merge; `documents: 'combine'` keeps provenance
    from every document the duplicates came from, which is what stops a merge
    from losing grounding.
    """
    await client.run(
        """
        MATCH (survivor:Entity {id: $survivor_id, workspace_id: $workspace_id})
        MATCH (dup:Entity {workspace_id: $workspace_id})
        WHERE dup.id IN $duplicate_ids
        WITH survivor, collect(dup) AS dups
        CALL apoc.refactor.mergeNodes(
            [survivor] + dups,
            {properties: {documents: 'combine', `.*`: 'discard'}, mergeRels: true}
        ) YIELD node
        WITH node
        // 'combine' can leave duplicate document ids; flatten them back out.
        SET node.documents = apoc.coll.toSet(
            CASE WHEN node.documents IS NULL THEN []
                 ELSE apoc.coll.flatten([node.documents]) END
        )
        RETURN node.id AS id
        """,
        workspace_id,
        survivor_id=survivor_id,
        duplicate_ids=duplicate_ids,
    )
