"""
Two pytincture rules that fail only at run time, checked here instead.

1. pytincture re-executes a BFF module's source on every call, so anything
   defined at module level there is new and empty each time. Sessions, tokens
   and the store must live in plain modules, and be the same objects on every
   call.
2. pytincture checks each JSON argument against the parameter's annotation and
   answers 400 on a mismatch: ``audio: int = None`` refuses the ``null`` the
   page sends. (This one cost a debugging session: the player just sat there.)
"""
import ast
import sys
from pathlib import Path

import pytest

APPCODE = Path(__file__).resolve().parents[1] / "appcode"
BFF_FILES = [p for p in (APPCODE / "services").glob("*.py") if "@backend_for_frontend" in p.read_text()]

loading = pytest.importorskip("pytincture.backend.source_loading")


def _load_like_pytincture(relative: str, hint: str):
    return loading.load_source_module(str(APPCODE / relative), hint, str(APPCODE))


def test_state_is_shared_across_calls():
    first = _load_like_pytincture("services/player_service.py", "PlayerService")
    second = _load_like_pytincture("services/player_service.py", "PlayerService")
    assert first is not second, "pytincture really does re-execute the module"
    assert first.transcode.sessions is second.transcode.sessions
    assert first.tokens is second.tokens is sys.modules["services.tokens"]


def test_a_token_made_in_one_call_is_known_to_the_media_routes(tmp_path):
    film = tmp_path / "a.mp4"
    film.write_bytes(b"x")
    module = _load_like_pytincture("services/player_service.py", "PlayerService")
    token = module.tokens.token_for(str(film))
    from services import tokens
    assert tokens.path_for(token) == str(film)


def test_bff_modules_keep_nothing_mutable_at_module_level():
    for path in BFF_FILES:
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                assert not isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Call)), (
                    f"{path.name}:{node.lineno} defines module-level state, which pytincture rebuilds on every call")


def test_parameters_that_default_to_none_accept_none():
    for path in BFF_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            args = node.args.args[-len(node.args.defaults):] if node.args.defaults else []
            for arg, default in zip(args, node.args.defaults):
                if isinstance(default, ast.Constant) and default.value is None and arg.annotation is not None:
                    text = ast.unparse(arg.annotation)
                    assert "None" in text or text.startswith("Optional"), (
                        f"{path.name}: {node.name}({arg.arg}: {text} = None) refuses null from the page")
