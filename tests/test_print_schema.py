"""Smoke test: the GraphQL schema must build and render to a non-empty SDL string.

No database required — this only imports and stringifies the schema.
"""

from bank_server.schema import schema


def test_print_schema():
    sdl = str(schema)
    print(sdl)  # visible with `pytest -s`
    assert sdl.strip(), "Schema SDL should not be empty"
