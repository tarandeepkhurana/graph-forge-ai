# Sample document — smoke test only

A tiny document with unambiguous facts, used to check that an extractor is wired up
correctly. It is **not** a benchmark: real evaluation uses `data/raw/` plus a corrected
gold set. If a model cannot find these relations, it is broken, not merely weak.

## Background

Marie Curie was a physicist and chemist who worked at the University of Paris. She
discovered the elements polonium and radium. Curie won the Nobel Prize in Physics in
1903, which she shared with Pierre Curie and Henri Becquerel. Her research on
radioactivity caused a lasting shift in atomic physics.

## Systems

Neo4j is a graph database that stores data as nodes and relationships. It uses Cypher
as its query language. GraphForge depends on Neo4j for graph storage and on PostgreSQL
for relational data. PostgreSQL is a relational database maintained by the PostgreSQL
Global Development Group.

FastAPI is a Python web framework created by Sebastian Ramirez. It is built on top of
Starlette and Pydantic. GraphForge uses FastAPI to serve both its HTML pages and its
JSON API.

## Measurements

The extraction pipeline measures precision and recall on each candidate model. Latency
is measured in seconds per chunk. The benchmark precedes the implementation phase.
