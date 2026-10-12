"""AIK Trusted Benchmark Engine.

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
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        elif msg_type == "new_game":
            board = chess.Board()
            resp = {
                "protocol_version": proto,
                "type": "ack",
                "request_id": req_id,
            }
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
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
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
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
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
