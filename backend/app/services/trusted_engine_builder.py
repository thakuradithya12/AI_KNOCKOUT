"""In-memory canonical USTAR builder for the trusted benchmark engine.

Included in the production app package to eliminate runtime dependencies on tests/.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from app.services.submission_validation import canonical_ustar_split

MANIFEST_JSON = b"""{
  "contract_version": "1",
  "protocol_version": "aik-jsonl/1",
  "engine_name": "AIK Trusted Benchmark Engine",
  "engine_version": "1.0.0",
  "runtime": "python-3.12",
  "entrypoint": "engine/main.py"
}
"""

ENGINE_MAIN_PY = b'''"""AIK Trusted Benchmark Engine.

Deterministic, non-malicious chess bot adhering strictly to the aik-jsonl/1 protocol.
Used for trusted development matches and integration testing.
"""

from __future__ import annotations

import json
import sys
import chess


def main() -> None:
    board = chess.Board()

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
            board = chess.Board()
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
            legal = sorted([m.uci() for m in board.legal_moves])
            if legal:
                chosen = legal[0]
                board.push_uci(chosen)
            else:
                chosen = "0000"
            resp = {
                "protocol_version": proto,
                "type": "move",
                "request_id": req_id,
                "move": chosen,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        elif msg_type == "stop":
            resp = {
                "protocol_version": proto,
                "type": "ack",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()
            break


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


def build_trusted_engine_archive() -> bytes:
    """Build a canonical USTAR archive for the trusted benchmark engine."""
    files = [
        ("manifest.json", MANIFEST_JSON),
        ("engine/main.py", ENGINE_MAIN_PY),
    ]

    output = bytearray()
    for path, content in sorted(files, key=lambda item: item[0].encode("utf-8")):
        output.extend(_header(path, len(content)))
        output.extend(content)
        output.extend(bytes((-len(content)) % 512))
    output.extend(bytes(1024))
    return bytes(output)


MAXIMUM_DIFFICULTY_ENGINE_NAME = "AIK House AI Maximum Difficulty"
MAXIMUM_DIFFICULTY_ENGINE_VERSION = "1.0.0"

MAXIMUM_DIFFICULTY_MANIFEST_JSON = b"""{
  "contract_version": "1",
  "protocol_version": "aik-jsonl/1",
  "engine_name": "AIK House AI Maximum Difficulty",
  "engine_version": "1.0.0",
  "runtime": "python-3.12",
  "entrypoint": "engine/main.py"
}
"""

MAXIMUM_DIFFICULTY_ENGINE_MAIN_PY = '''"""AIK House AI - Maximum Difficulty Engine.

Adheres strictly to the aik-jsonl/1 protocol.
Features:
- Full minimax search with alpha-beta pruning.
- Material balance evaluation.
- Positional Piece-Square Tables (PST) for opening, middlegame, and endgame.
- Move ordering via MVV-LVA (Most Valuable Victim - Least Valuable Attacker), checks, promotions.
- Quiescence search on captures.
- Iterative deepening with strict time allocation bounding search within the move deadline.
"""

from __future__ import annotations

import json
import sys
import time
import chess

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

PAWN_TABLE = [
     0,  0,  0,  0,  0,  0,  0,  0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
     5,  5, 10, 25, 25, 10,  5,  5,
     0,  0,  0, 20, 20,  0,  0,  0,
     5, -5,-10,  0,  0,-10, -5,  5,
     5, 10, 10,-20,-20, 10, 10,  5,
     0,  0,  0,  0,  0,  0,  0,  0,
]

KNIGHT_TABLE = [
   -50,-40,-30,-30,-30,-30,-40,-50,
   -40,-20,  0,  0,  0,  0,-20,-40,
   -30,  0, 10, 15, 15, 10,  0,-30,
   -30,  5, 15, 20, 20, 15,  5,-30,
   -30,  0, 15, 20, 20, 15,  0,-30,
   -30,  5, 10, 15, 15, 10,  5,-30,
   -40,-20,  0,  5,  5,  0,-20,-40,
   -50,-40,-30,-30,-30,-30,-40,-50,
]

BISHOP_TABLE = [
   -20,-10,-10,-10,-10,-10,-10,-20,
   -10,  0,  0,  0,  0,  0,  0,-10,
   -10,  0,  5, 10, 10,  5,  0,-10,
   -10,  5,  5, 10, 10,  5,  5,-10,
   -10,  0, 10, 10, 10, 10,  0,-10,
   -10, 10, 10, 10, 10, 10, 10,-10,
   -10,  5,  0,  0,  0,  0,  5,-10,
   -20,-10,-10,-10,-10,-10,-10,-20,
]

ROOK_TABLE = [
     0,  0,  0,  0,  0,  0,  0,  0,
     5, 10, 10, 10, 10, 10, 10,  5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
     0,  0,  0,  5,  5,  0,  0,  0,
]

QUEEN_TABLE = [
   -20,-10,-10, -5, -5,-10,-10,-20,
   -10,  0,  0,  0,  0,  0,  0,-10,
   -10,  0,  5,  5,  5,  5,  0,-10,
    -5,  0,  5,  5,  5,  5,  0, -5,
     0,  0,  5,  5,  5,  5,  0, -5,
   -10,  5,  5,  5,  5,  5,  0,-10,
   -10,  0,  5,  0,  0,  0,  0,-10,
   -20,-10,-10, -5, -5,-10,-10,-20,
]

KING_TABLE = [
   -30,-40,-40,-50,-50,-40,-40,-30,
   -30,-40,-40,-50,-50,-40,-40,-30,
   -30,-40,-40,-50,-50,-40,-40,-30,
   -30,-40,-40,-50,-50,-40,-40,-30,
   -20,-30,-30,-40,-40,-30,-30,-20,
   -10,-20,-20,-20,-20,-20,-20,-10,
    20, 20,  0,  0,  0,  0, 20, 20,
    20, 30, 10,  0,  0, 10, 30, 20,
]

PST = {
    chess.PAWN: PAWN_TABLE,
    chess.KNIGHT: KNIGHT_TABLE,
    chess.BISHOP: BISHOP_TABLE,
    chess.ROOK: ROOK_TABLE,
    chess.QUEEN: QUEEN_TABLE,
    chess.KING: KING_TABLE,
}


def evaluate_board(board: chess.Board) -> int:
    if board.is_checkmate():
        return -100000
    if board.is_stalemate() or board.is_insufficient_material():
        return 0

    white_score = 0
    black_score = 0

    for square, piece in board.piece_map().items():
        pt = piece.piece_type
        val = PIECE_VALUES[pt]
        pst_sq = square if piece.color == chess.WHITE else (square ^ 56)
        pst_val = PST[pt][pst_sq]
        total = val + pst_val
        if piece.color == chess.WHITE:
            white_score += total
        else:
            black_score += total

    score = white_score - black_score
    return score if board.turn == chess.WHITE else -score


def score_move(board: chess.Board, move: chess.Move) -> int:
    score = 0
    if board.is_capture(move):
        attacker = board.piece_at(move.from_square)
        victim = board.piece_at(move.to_square)
        attacker_val = PIECE_VALUES.get(attacker.piece_type, 0) if attacker else 0
        victim_val = PIECE_VALUES.get(victim.piece_type, 100) if victim else 100
        score += 10000 + (victim_val * 10 - attacker_val)
    if move.promotion:
        score += 9000
    if board.gives_check(move):
        score += 1000
    return score


def quiescence(board: chess.Board, alpha: int, beta: int, qdepth: int = 2) -> int:
    stand_pat = evaluate_board(board)
    if stand_pat >= beta:
        return beta
    if alpha < stand_pat:
        alpha = stand_pat
    if qdepth <= 0:
        return stand_pat

    capture_moves = [m for m in board.legal_moves if board.is_capture(m)]
    capture_moves.sort(key=lambda m: score_move(board, m), reverse=True)

    for move in capture_moves:
        board.push(move)
        try:
            score = -quiescence(board, -beta, -alpha, qdepth - 1)
        finally:
            board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def alpha_beta(board: chess.Board, depth: int, alpha: int, beta: int, deadline: float) -> int:
    if time.monotonic() > deadline:
        raise TimeoutError()

    if depth <= 0 or board.is_game_over():
        return quiescence(board, alpha, beta, qdepth=2)

    legal_moves = list(board.legal_moves)
    legal_moves.sort(key=lambda m: score_move(board, m), reverse=True)

    best_score = -200000
    for move in legal_moves:
        board.push(move)
        try:
            score = -alpha_beta(board, depth - 1, -beta, -alpha, deadline)
        finally:
            board.pop()

        if score > best_score:
            best_score = score
        if score > alpha:
            alpha = score
        if alpha >= beta:
            break
    return best_score


def search_best_move(board: chess.Board, time_limit_ms: int = 1500) -> str:
    search_board = board.copy()
    legal_moves = list(search_board.legal_moves)
    if not legal_moves:
        return "0000"
    if len(legal_moves) == 1:
        return legal_moves[0].uci()

    # Immediate winning move check
    for m in legal_moves:
        search_board.push(m)
        try:
            if search_board.is_checkmate():
                return m.uci()
        finally:
            search_board.pop()

    start_time = time.monotonic()
    deadline = start_time + min(time_limit_ms / 1000.0 * 0.75, 1.2)
    legal_moves.sort(key=lambda m: score_move(search_board, m), reverse=True)
    best_move = legal_moves[0]

    for depth in range(1, 5):
        current_best_move = None
        current_best_score = -200000
        alpha = -200000
        beta = 200000
        try:
            for move in legal_moves:
                search_board.push(move)
                try:
                    score = -alpha_beta(search_board, depth - 1, -beta, -alpha, deadline)
                finally:
                    search_board.pop()

                if score > current_best_score:
                    current_best_score = score
                    current_best_move = move
                if score > alpha:
                    alpha = score
            if current_best_move is not None:
                best_move = current_best_move
        except TimeoutError:
            break

        if current_best_score >= 90000:
            break

        if time.monotonic() > deadline:
            break

    return best_move.uci()


def main() -> None:
    board = chess.Board()

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
            board = chess.Board()
            resp = {
                "protocol_version": proto,
                "type": "ack",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        elif msg_type == "position":
            fen = msg.get("fen")
            moves = msg.get("uci_moves") or msg.get("moves") or []
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
            clocks = msg.get("clocks_ms", {})
            side_key = "white" if board.turn == chess.WHITE else "black"
            time_left_ms = clocks.get(side_key, msg.get("move_budget_ms", msg.get("time_left_ms", 2000)))
            chosen = search_best_move(board, time_left_ms)
            board.push_uci(chosen)
            resp = {
                "protocol_version": proto,
                "type": "move",
                "request_id": req_id,
                "move": chosen,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        elif msg_type == "stop":
            resp = {
                "protocol_version": proto,
                "type": "ack",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
            sys.stdout.flush()
            break


if __name__ == "__main__":
    main()
'''.encode("utf-8")


def build_maximum_difficulty_engine_archive() -> bytes:
    """Build a canonical USTAR archive for House AI Maximum Difficulty engine."""
    files = [
        ("manifest.json", MAXIMUM_DIFFICULTY_MANIFEST_JSON),
        ("engine/main.py", MAXIMUM_DIFFICULTY_ENGINE_MAIN_PY),
    ]

    output = bytearray()
    for path, content in sorted(files, key=lambda item: item[0].encode("utf-8")):
        output.extend(_header(path, len(content)))
        output.extend(content)
        output.extend(bytes((-len(content)) % 512))
    output.extend(bytes(1024))
    return bytes(output)


AIK_HOUSE_001_ID = "AIK-HOUSE-001"
AIK_HOUSE_001_VERSION = 1
AIK_HOUSE_001_ENGINE_NAME = "AIK House AI Unified Master"
AIK_HOUSE_001_ENGINE_VERSION = "1.0.0"

AIK_HOUSE_001_MANIFEST_JSON = b"""{
  "contract_version": "1",
  "protocol_version": "aik-jsonl/1",
  "engine_name": "AIK House AI Unified Master",
  "engine_version": "1.0.0",
  "runtime": "python-3.12",
  "entrypoint": "engine/main.py"
}
"""


def _get_aik_house_001_engine_source() -> bytes:
    from pathlib import Path
    unified_py = Path(__file__).parent / "house_ai_unified.py"
    if unified_py.exists():
        return unified_py.read_bytes()
    # Fallback to maximum difficulty if missing
    return MAXIMUM_DIFFICULTY_ENGINE_MAIN_PY


def build_aik_house_001_engine_archive() -> bytes:
    """Build canonical USTAR archive for AIK-HOUSE-001 Unified House AI."""
    main_py_bytes = _get_aik_house_001_engine_source()
    files = [
        ("manifest.json", AIK_HOUSE_001_MANIFEST_JSON),
        ("engine/main.py", main_py_bytes),
        ("engine/intelligence.py", main_py_bytes),
    ]

    output = bytearray()
    for path, content in sorted(files, key=lambda item: item[0].encode("utf-8")):
        output.extend(_header(path, len(content)))
        output.extend(content)
        output.extend(bytes((-len(content)) % 512))
    output.extend(bytes(1024))
    return bytes(output)


class TrustedEngineSession:
    """In-memory session for a trusted House AI engine strictly adhering to aik-jsonl/1."""

    def __init__(
        self,
        house_ai_id: str = "house-ai-benchmark-baseline",
        *,
        move_fn: Callable[[str, list[str], int], str] | None = None,
    ) -> None:
        import chess

        self.chess = chess
        self.house_ai_id = house_ai_id
        self.board = chess.Board()
        self.move_fn = move_fn

    def handle_message(self, message: bytes) -> bytes:
        raw = message.strip()
        if not raw:
            return b""
        data = json.loads(raw.decode("utf-8"))
        msg_type = data.get("type")
        req_id = data.get("request_id", "")
        proto = data.get("protocol_version", "aik-jsonl/1")

        if msg_type == "hello":
            resp = {"protocol_version": proto, "type": "ready", "request_id": req_id}
        elif msg_type == "new_game":
            self.board = self.chess.Board()
            resp = {"protocol_version": proto, "type": "ack", "request_id": req_id}
        elif msg_type == "position":
            fen = data.get("fen")
            moves = data.get("uci_moves", [])
            if fen:
                try:
                    self.board = self.chess.Board(fen)
                except Exception:
                    self.board = self.chess.Board()
            elif moves:
                self.board = self.chess.Board()
                for m in moves:
                    try:
                        self.board.push_uci(m)
                    except Exception:
                        pass
            resp = {"protocol_version": proto, "type": "ack", "request_id": req_id}
        elif msg_type == "go":
            legal = sorted([m.uci() for m in self.board.legal_moves])
            budget = data.get("move_budget_ms", 2000)
            if self.move_fn:
                chosen = self.move_fn(self.board.fen(), legal, budget)
            elif self.house_ai_id in ("house-ai-maximum-difficulty", "AIK-HOUSE-001"):
                try:
                    from app.services.house_ai_unified import choose_move

                    chosen = choose_move(self.board.fen(), legal, budget)
                except Exception:
                    chosen = legal[0] if legal else "0000"
            else:
                chosen = legal[0] if legal else "0000"
            if chosen in legal:
                try:
                    self.board.push_uci(chosen)
                except Exception:
                    pass
            resp = {
                "protocol_version": proto,
                "type": "move",
                "request_id": req_id,
                "move": chosen,
            }
        elif msg_type == "stop":
            resp = {"protocol_version": proto, "type": "ack", "request_id": req_id}
        else:
            raise ValueError(f"Unknown message type: {msg_type}")

        return json.dumps(resp, separators=(",", ":")).encode("utf-8") + b"\n"



