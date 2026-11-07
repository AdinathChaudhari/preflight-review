"""Release tag helpers."""


def tags_for(prefix, tags):
    return [t for t in tags if t.startswith(prefix)]


def latest_tag(tags):
    """Newest release tag, e.g. 'v1.10.0' after 'v1.9.0'."""
    return sorted(tags)[-1]
