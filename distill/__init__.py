"""Teacher-student distillation data for a small interpreter (docs/design/small-interpreter.md §5).

`spec` holds the versioned rules (scenario categories, voice variants, the outcome the
production prompt's own rules predict for each), `voice` samples SER hints the way
emotion2vec emits them, and `pipeline` runs the resumable phases. How to run it:
docs/guides/distill.md.
"""
