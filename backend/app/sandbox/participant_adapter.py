"""Server-owned adapter bridging aik-jsonl/1 protocol to participant intelligence.py.

Supplies the execution harness inside the sandbox so participants only need to write
a single Python file (intelligence.py) implementing their chess-playing strategy.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Optional

from app.services.submission_validation import PackageLimits, PackageRejected, canonical_ustar_split

# Standalone source code of the server-owned adapter that runs as engine/main.py inside the sandbox
PARTICIPANT_ADAPTER_SOURCE = '''"""AI Knockout Server-Owned Participant Adapter (engine/main.py).

Translates aik-jsonl/1 worker protocol to participant intelligence.py API.
Runs strictly inside the isolated sandbox.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import traceback

try:
    import chess
except ImportError:
    chess = None

# Ensure current working directory and engine directory are on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.abspath("."), os.path.abspath("engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Import participant intelligence module
_intelligence = None
try:
    import intelligence
    _intelligence = intelligence
except Exception as _import_err:
    try:
        from engine import intelligence as _eng_intel
        _intelligence = _eng_intel
    except Exception as _import_err2:
        sys.stderr.write(f"Failed to import intelligence.py: {_import_err}\\n{_import_err2}\\n")
        sys.stderr.flush()


def _resolve_move_callable():
    if _intelligence is None:
        return None

    # 1. Top-level functions
    for fn_name in ("choose_move", "play_move", "make_move", "get_move"):
        fn = getattr(_intelligence, fn_name, None)
        if callable(fn):
            return fn

    # 2. Class-based agent
    for cls_name in ("ChessAgent", "Agent", "Player", "Bot", "Intelligence"):
        cls = getattr(_intelligence, cls_name, None)
        if isinstance(cls, type):
            try:
                instance = cls()
                for fn_name in ("choose_move", "play_move", "make_move", "get_move"):
                    fn = getattr(instance, fn_name, None)
                    if callable(fn):
                        return fn
            except Exception:
                pass
    return None


def main() -> None:
    board = chess.Board() if chess is not None else None
    agent_fn = _resolve_move_callable()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        msg_type = msg.get("type")
        req_id = msg.get("request_id", "")
        proto = msg.get("protocol_version", "aik-jsonl/1")

        if msg_type == "hello":
            resp = {
                "protocol_version": proto,
                "type": "ready",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        elif msg_type == "new_game":
            if chess is not None:
                board = chess.Board()
            if _intelligence and hasattr(_intelligence, "new_game") and callable(_intelligence.new_game):
                try:
                    _intelligence.new_game()
                except Exception:
                    pass
            resp = {
                "protocol_version": proto,
                "type": "ack",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        elif msg_type == "position":
            fen = msg.get("fen")
            moves = msg.get("uci_moves", [])
            if chess is not None:
                if fen:
                    try:
                        board = chess.Board(fen)
                    except Exception:
                        board = chess.Board()
                elif moves:
                    board = chess.Board()
                    for m in moves:
                        try:
                            board.push_uci(m)
                        except Exception:
                            pass
            resp = {
                "protocol_version": proto,
                "type": "ack",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        elif msg_type == "go":
            board_fen = board.fen() if board else "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            legal_moves = sorted([m.uci() for m in board.legal_moves]) if board else []
            clocks = msg.get("clocks_ms", {})
            side_key = "white" if (board and board.turn == chess.WHITE) else "black"
            time_left_ms = clocks.get(side_key, msg.get("move_budget_ms", 3000))

            chosen_move = "0000"
            if agent_fn is not None:
                try:
                    sig = inspect.signature(agent_fn)
                    params = len(sig.parameters)
                    if params >= 3:
                        chosen_move = agent_fn(board_fen, legal_moves, time_left_ms)
                    elif params == 2:
                        chosen_move = agent_fn(board_fen, legal_moves)
                    elif params == 1:
                        chosen_move = agent_fn(board_fen)
                    else:
                        chosen_move = agent_fn()
                    if not isinstance(chosen_move, str):
                        chosen_move = str(chosen_move)
                except Exception as exc:
                    sys.stderr.write(f"Agent error in intelligence.py: {exc}\\n{traceback.format_exc()}\\n")
                    sys.stderr.flush()
                    chosen_move = "0000"

            resp = {
                "protocol_version": proto,
                "type": "bestmove",
                "request_id": req_id,
                "move": chosen_move,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
'''


def _octal(value: int, length: int) -> bytes:
    return format(value, f"0{length - 1}o").encode("ascii") + b"\0"


def _header(path: str, size: int) -> bytes:
    prefix, name = canonical_ustar_split(path)
    block = bytearray(512)
    block[: len(name)] = name
    block[100:108] = _octal(0o644, 8)
    block[108:116] = _octal(0, 8)
    block[116:124] = _octal(0, 8)
    block[124:136] = _octal(size, 12)
    block[136:148] = _octal(0, 12)
    block[148:156] = b" " * 8
    block[156:157] = b"0"
    block[257:263] = b"ustar\0"
    block[263:265] = b"00"
    block[345 : 345 + len(prefix)] = prefix
    checksum = sum(block)
    block[148:156] = f"{checksum:06o}".encode("ascii") + b"\0 "
    return bytes(block)


def build_canonical_intelligence_package(
    intelligence_code: bytes,
    engine_name: str = "ParticipantAgent",
) -> bytes:
    """Build a canonical USTAR archive embedding intelligence.py and the server adapter."""
    # 1. UTF-8 decode & BOM check
    if intelligence_code.startswith(b"\xef\xbb\xbf"):
        raise PackageRejected("SOURCE_BOM_FORBIDDEN", "VALIDATING")
    try:
        source_text = intelligence_code.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackageRejected("SOURCE_ENCODING_INVALID", "VALIDATING") from exc

    # 2. Syntax validation and strategy entrypoint check via ast.parse (safe static check without code execution)
    try:
        tree = ast.parse(source_text, filename="intelligence.py")
    except (SyntaxError, ValueError) as exc:
        raise PackageRejected("PYTHON_SYNTAX_ERROR", "VALIDATING") from exc

    has_entrypoint = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
            "choose_move", "play_move", "make_move", "get_move"
        ):
            has_entrypoint = True
            break
        elif isinstance(node, ast.ClassDef) and node.name in (
            "ChessAgent", "Agent", "Player", "Bot", "Intelligence"
        ):
            has_entrypoint = True
            break
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "choose_move", "play_move", "make_move", "get_move"
                ):
                    has_entrypoint = True
                    break
    if not has_entrypoint:
        raise PackageRejected("INTELLIGENCE_ENTRYPOINT_MISSING", "VALIDATING")

    # 3. Canonical Manifest
    manifest_dict = {
        "contract_version": "1",
        "protocol_version": "aik-jsonl/1",
        "engine_name": engine_name,
        "engine_version": "1.0.0",
        "runtime": "python-3.12",
        "entrypoint": "engine/main.py",
    }
    manifest_bytes = json.dumps(manifest_dict, indent=2, sort_keys=True).encode("utf-8")
    adapter_bytes = PARTICIPANT_ADAPTER_SOURCE.encode("utf-8")
    readme_bytes = b"# AI Knockout Participant Agent\n\nAutomated package containing participant intelligence.py.\n"

    # Sorted ASCII member order:
    # 1. README.md
    # 2. engine/intelligence.py
    # 3. engine/main.py
    # 4. manifest.json
    files = [
        ("README.md", readme_bytes),
        ("engine/intelligence.py", intelligence_code),
        ("engine/main.py", adapter_bytes),
        ("manifest.json", manifest_bytes),
    ]

    output = bytearray()
    for path, content in sorted(files, key=lambda item: item[0].encode("ascii")):
        output.extend(_header(path, len(content)))
        output.extend(content)
        rem = len(content) % 512
        if rem != 0:
            output.extend(bytes(512 - rem))
    output.extend(bytes(1024))
    return bytes(output)


def is_ustar_archive(data: bytes) -> bool:
    """Check if bytes represent a valid POSIX USTAR archive."""
    if len(data) < 1024 or len(data) % 512 != 0:
        return False
    # Check USTAR magic at offset 257 in first header
    return data[257:263] == b"ustar\0" and data[263:265] == b"00"


def wrap_submission_if_needed(
    data: bytes, limits: PackageLimits, content_type: str | None = None
) -> bytes:
    """Wrap raw intelligence.py or partial tar archives into a canonical USTAR package.

    Preserves 100% backward compatibility for already-canonical packages and standard tar validation.
    """
    if len(data) > limits.max_archive_bytes:
        raise PackageRejected("PACKAGE_TOO_LARGE")

    # If explicitly declared as a tar archive:
    if content_type in ("application/x-tar", "application/tar"):
        if is_ustar_archive(data):
            from app.services.submission_validation import _parse_archive
            try:
                files = _parse_archive(data, limits)
            except Exception:
                return data

            if "manifest.json" in files and "engine/main.py" in files:
                return data

            intel_code = files.get("intelligence.py") or files.get("engine/intelligence.py")
            if intel_code is not None:
                return build_canonical_intelligence_package(intel_code)

        # Not a valid USTAR archive -> pass through to archive validator to report ARCHIVE_MALFORMED
        return data

    # If explicitly declared as Python code:
    if content_type in ("text/x-python", "text/x-script.python"):
        return build_canonical_intelligence_package(data)

    # If it is a USTAR archive:
    if is_ustar_archive(data):
        from app.services.submission_validation import _parse_archive
        try:
            files = _parse_archive(data, limits)
        except Exception:
            return data

        if "manifest.json" in files and "engine/main.py" in files:
            return data

        intel_code = files.get("intelligence.py") or files.get("engine/intelligence.py")
        if intel_code is not None:
            return build_canonical_intelligence_package(intel_code)

        return data

    # If binary data containing null bytes:
    if b"\0" in data:
        return data

    if content_type in ("text/plain",):
        return build_canonical_intelligence_package(data)

    # If content_type is octet-stream or omitted without null bytes, check for Python entrypoint keywords
    text_sample = data[:2048]
    if any(kw in text_sample for kw in (b"choose_move", b"play_move", b"make_move", b"ChessAgent", b"Agent", b"Player", b"Bot", b"Intelligence")):
        return build_canonical_intelligence_package(data)

    # Otherwise pass through to standard archive validator
    return data

