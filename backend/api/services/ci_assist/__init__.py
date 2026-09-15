"""Assisted CI configuration: work out what a repository is, and propose a build.

This package is the *only* place in KubeSight where a model is asked about a
pipeline, and the dependency deliberately points one way:

    ci_assist  ──imports──▶  services/ci        (catalog, pipelines, validator)
    services/ci  ──never──▶  ci_assist

``services/ci`` states in its own docstring that it imports no Hermes and no AI
code path. That is not tidiness — it is the guarantee that a build cannot depend
on a model being reachable. Keeping this package outside it, and the *validator*
inside it, is what makes the guarantee structural rather than a rule someone has
to remember.

The division of labour, end to end:

    evidence.py   reads the repository (no clone — the source port's REST reads)
    hermes.py     asks the model, over a versioned, strictly-validated contract
    profile.py    what the application IS, and the application_type it derives
    generator.py  orchestrates: evidence → propose → validate → repair → persist
    jobs.py       runs that off the request thread, and reaps what dies
    accept.py     turns an approved proposal into ordinary KubeSight rows

Everything the model returns is untrusted configuration until
``services/ci/generated.validate`` has passed it, and untrusted *still* until a
person has approved it. Neither gate is optional.
"""
