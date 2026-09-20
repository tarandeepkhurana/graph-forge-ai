"""The map has to be self-consistent before it reaches the graph.

Every check here exists because the old pipeline shipped the failure straight
to the canvas: an edge pointing at a concept that was never created, a
prerequisite pointing at itself, the same idea under two capitalisations. The
model that fills this in is good but not reliable, and the canvas has no way to
recover from a malformed map.
"""

from graphforge.extraction.conceptmap import (
    Concept, ConceptMap, Contrast, Link, _batches, _sections, _short_name, tidy,
)


def _map(**kw):
    return ConceptMap(**kw)


def test_duplicate_concepts_fold_together():
    m = tidy(_map(concepts=[
        Concept(name="Online Learning", strengths=["fast"]),
        Concept(name="online learning", importance="core", limitations=["noisy"]),
    ]))
    assert len(m.concepts) == 1
    assert m.concepts[0].importance == "core"       # core survives the merge
    assert m.concepts[0].strengths == ["fast"]
    assert m.concepts[0].limitations == ["noisy"]


def test_links_to_missing_concepts_are_dropped():
    m = tidy(_map(
        concepts=[Concept(name="A"), Concept(name="B")],
        links=[Link(source="A", target="B", relation="trains on"),
               Link(source="A", target="Ghost", relation="uses")],
    ))
    assert [l.target for l in m.links] == ["B"]


def test_vague_relations_are_dropped():
    m = tidy(_map(
        concepts=[Concept(name="A"), Concept(name="B")],
        links=[Link(source="A", target="B", relation="related_to"),
               Link(source="A", target="B", relation="discards")],
    ))
    assert [l.relation for l in m.links] == ["discards"]


def test_a_concept_never_requires_itself():
    m = tidy(_map(concepts=[Concept(name="A", requires=["A", "a"])]))
    assert m.concepts[0].requires == []


def test_prerequisite_cycles_are_broken():
    # "read A first, but read B before A" leaves the flow view with no root.
    m = tidy(_map(concepts=[
        Concept(name="A", requires=["B"]),
        Concept(name="B", requires=["C"]),
        Concept(name="C", requires=["A"]),
    ]))
    edges = {(c.name, r) for c in m.concepts for r in c.requires}
    assert len(edges) == 2, edges

    # and what survives must be acyclic
    reach = {c.name: set(c.requires) for c in m.concepts}
    for _ in range(4):
        for name in reach:
            reach[name] |= {r for n in list(reach[name]) for r in reach.get(n, ())}
    assert all(name not in reach[name] for name in reach)


def test_core_concepts_survive_the_cap():
    concepts = [Concept(name=f"detail{i}") for i in range(20)]
    concepts.append(Concept(name="The Point", importance="core"))
    m = tidy(_map(concepts=concepts), cap=5)
    assert len(m.concepts) == 5
    assert m.concepts[0].name == "The Point"


def test_contrast_of_one_thing_is_not_a_contrast():
    m = tidy(_map(
        concepts=[Concept(name="A"), Concept(name="B")],
        contrasts=[Contrast(dimension="speed", values={"A": "fast", "Ghost": "slow"}),
                   Contrast(dimension="memory", values={"A": "low", "B": "high"})],
    ))
    assert [c.dimension for c in m.contrasts] == ["memory"]


def test_sections_split_on_headings_not_mid_sentence():
    text = "# One\nalpha beta\n\n# Two\ngamma delta"
    parts = _sections(text)
    assert len(parts) == 2
    assert parts[0].startswith("# One")
    assert "gamma" in parts[1]


def test_batches_group_whole_sections_under_budget():
    parts = ["x" * 4000, "y" * 4000, "z" * 4000]   # ~1000 tokens each
    assert len(_batches(parts, 2500)) == 2         # 2 + 1
    assert len(_batches(parts, 100_000)) == 1      # all together


def test_long_names_are_trimmed_to_a_caption():
    # These are real names the model returned; both wrap to several lines
    # under a node and collide with whatever is next to them.
    assert _short_name("Full Retraining (Batch Learning: Retrain from scratch)") == "Full Retraining"
    # 44 characters -- under any sane cap, still unreadable under a node.
    assert _short_name("partial_fit() / SGD (incremental update API)") == "partial_fit"
    assert _short_name("Naive Bayes (Incremental Version)") == "Naive Bayes"
    assert _short_name("Online Learning") == "Online Learning"
    # No parenthesis to cut: trim on a word boundary, never mid-word.
    long = _short_name("a " * 40)
    assert len(long) <= 46 and not long.endswith(" ")


def test_links_follow_a_shortened_name():
    # The model writes links against the long form it invented; if the rename
    # is not followed the edge points at a concept that no longer exists.
    m = tidy(_map(
        concepts=[Concept(name="Full Retraining (Batch Learning: Retrain from scratch)"),
                  Concept(name="Batch Machine Learning")],
        links=[Link(source="Batch Machine Learning",
                    target="Full Retraining (Batch Learning: Retrain from scratch)",
                    relation="uses")],
    ))
    assert [c.name for c in m.concepts] == ["Full Retraining", "Batch Machine Learning"] or            sorted(c.name for c in m.concepts) == ["Batch Machine Learning", "Full Retraining"]
    assert len(m.links) == 1
    assert m.links[0].target == "Full Retraining"


def test_prerequisites_follow_a_shortened_name():
    m = tidy(_map(concepts=[
        Concept(name="partial_fit() / SGD (incremental update API)",
                requires=["Online Learning"]),
        Concept(name="Online Learning"),
    ]))
    pf = [c for c in m.concepts if c.name == "partial_fit"][0]
    assert pf.requires == ["Online Learning"]


def test_core_concepts_do_not_depend_on_supporting_ones():
    # Observed: "Batch Machine Learning requires Full retraining", alongside
    # "Full retraining implements Batch Machine Learning" in the same run.
    # Supporting concepts live inside the subject, so that arrow is backwards.
    m = tidy(_map(concepts=[
        Concept(name="Batch Machine Learning", importance="core",
                requires=["Full retraining"]),
        Concept(name="Full retraining", importance="supporting"),
    ]))
    batch = [c for c in m.concepts if c.name == "Batch Machine Learning"][0]
    assert batch.requires == []


def test_supporting_may_still_depend_on_core():
    m = tidy(_map(concepts=[
        Concept(name="Online Learning", importance="core"),
        Concept(name="partial_fit", importance="supporting",
                requires=["Online Learning"]),
    ]))
    pf = [c for c in m.concepts if c.name == "partial_fit"][0]
    assert pf.requires == ["Online Learning"]


def test_core_may_depend_on_core():
    m = tidy(_map(concepts=[
        Concept(name="Online Learning", importance="core"),
        Concept(name="Incremental Learning", importance="core",
                requires=["Online Learning"]),
    ]))
    inc = [c for c in m.concepts if c.name == "Incremental Learning"][0]
    assert inc.requires == ["Online Learning"]
