"""Version-pinned python-chess implementation behind the match rules port."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.match import MatchOutcome

if TYPE_CHECKING:
    import chess


class ChessRulesAdapter:
    """Server-authoritative standard chess rules using python-chess 1.11.2."""

    library_version = "1.11.2"
    rules_version = "standard-chess/python-chess-1.11.2"

    def __init__(self, initial_fen: str, rules_version: str = rules_version) -> None:
        if rules_version != self.rules_version:
            raise RuntimeError("CHESS_RULES_CONFIGURATION_VERSION_MISMATCH")
        import chess

        if chess.__version__ != self.library_version:
            raise RuntimeError("CHESS_RULES_LIBRARY_VERSION_MISMATCH")
        try:
            board = chess.Board(initial_fen)
        except (ValueError, TypeError) as exc:
            raise ValueError("CHESS_INITIAL_POSITION_INVALID") from exc
        if not board.is_valid():
            raise ValueError("CHESS_INITIAL_POSITION_INVALID")
        self._chess = chess
        self._initial_fen = self._serialize_board(board)

    @property
    def initial_position(self) -> str:
        return self._initial_fen

    def serialize_position(self, position: str, history: tuple[str, ...] = ()) -> str:
        return self._serialize_board(self._board(position, history))

    def deserialize_position(self, encoded: str, history: tuple[str, ...] = ()) -> str:
        board = self._board(encoded, history)
        canonical = self._serialize_board(board)
        if canonical != encoded:
            raise ValueError("CHESS_POSITION_NOT_CANONICAL")
        return canonical

    def side_to_move(self, position: str, history: tuple[str, ...] = ()) -> str:
        board = self._board(position, history)
        return "WHITE" if board.turn == self._chess.WHITE else "BLACK"

    def is_check(self, position: str, history: tuple[str, ...] = ()) -> bool:
        return self._board(position, history).is_check()

    def is_checkmate(self, position: str, history: tuple[str, ...] = ()) -> bool:
        return self._board(position, history).is_checkmate()

    def is_stalemate(self, position: str, history: tuple[str, ...] = ()) -> bool:
        return self._board(position, history).is_stalemate()

    def is_insufficient_material(self, position: str, history: tuple[str, ...] = ()) -> bool:
        return self._board(position, history).is_insufficient_material()

    def apply_legal_move(
        self, position: str, move_uci: str, history: tuple[str, ...]
    ) -> str | None:
        board = self._board(position, history)
        try:
            move = self._chess.Move.from_uci(move_uci)
        except (ValueError, TypeError):
            return None
        if move not in board.legal_moves:
            return None
        board.push(move)
        return self._serialize_board(board)

    def terminal_result(
        self, position: str, history: tuple[str, ...]
    ) -> tuple[MatchOutcome, str] | None:
        board = self._board(position, history)
        if board.is_checkmate():
            winner = (
                MatchOutcome.BLACK_WIN
                if board.turn == self._chess.WHITE
                else MatchOutcome.WHITE_WIN
            )
            return winner, "CHECKMATE"
        if board.is_stalemate():
            return MatchOutcome.DRAW, "STALEMATE"
        if board.is_insufficient_material():
            return MatchOutcome.DRAW, "DEAD_POSITION"
        if board.is_fivefold_repetition():
            return MatchOutcome.DRAW, "FIVEFOLD_REPETITION"
        if board.is_seventyfive_moves():
            return MatchOutcome.DRAW, "SEVENTY_FIVE_MOVE_RULE"
        return None

    def verify_draw_claim(
        self, position: str, history: tuple[str, ...], reason: str, intended_move: str | None
    ) -> bool:
        board = self._board(position, history)
        claim_board = board.copy(stack=True)
        if intended_move is not None:
            try:
                move = self._chess.Move.from_uci(intended_move)
            except (ValueError, TypeError):
                return False
            if move not in board.legal_moves:
                return False
            claim_board.push(move)
        if reason == "threefold_repetition":
            return claim_board.can_claim_threefold_repetition()
        if reason == "fifty_move_rule":
            return claim_board.can_claim_fifty_moves()
        return False

    def _board(self, position: str, history: tuple[str, ...]):
        chess = self._chess
        try:
            board = chess.Board(self._initial_fen)
            for move_uci in history:
                move = chess.Move.from_uci(move_uci)
                if move not in board.legal_moves:
                    raise ValueError("CHESS_TRANSCRIPT_MOVE_ILLEGAL")
                board.push(move)
            if not board.is_valid() or self._serialize_board(board) != position:
                raise ValueError("CHESS_POSITION_HISTORY_MISMATCH")
            return board
        except (ValueError, TypeError) as exc:
            if str(exc).startswith("CHESS_"):
                raise
            raise ValueError("CHESS_POSITION_INVALID") from exc

    @staticmethod
    def _serialize_board(board: chess.Board) -> str:
        return board.fen(en_passant="fen")
