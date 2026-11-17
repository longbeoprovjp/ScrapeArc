"""Importer plugins. Each is a function  run(c, args) -> stats dict, registered by name.

To add a new source: create a module here, decorate a function with @register("name"),
and give it an `add_arguments(parser)` function if it needs CLI options.
"""
REGISTRY = {}


def register(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


from . import folder, gallerydl, htmlscrape  # noqa: E402,F401  (import for registration side effect)
