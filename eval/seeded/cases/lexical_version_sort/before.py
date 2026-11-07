"""Release tag helpers."""


def tags_for(prefix, tags):
    return [t for t in tags if t.startswith(prefix)]
