# AI Knockout — Event Competition Rules and Participant Submission Specification
**Event:** AI Knockout — MVSR Engineering College  
**Document Version:** 1.0 (Pre-Infrastructure Procurement)  
**Status:** Authoritative Specification

---

## 1. Executive Summary & Competition Philosophy

AI Knockout is a high-integrity, server-governed competitive programming tournament where participants construct autonomous AI engines to play standard chess. The platform guarantees absolute competitive fairness:
- **Server Authoritative:** Move legality, clock countdowns, timeouts, draw declarations, and game results are exclusively determined by the central event coordinator using `python-chess 1.11.2`.
- **Zero Hallucination:** No match result or leaderboard rank is ever fabricated or estimated.
- **Air-Gapped Execution:** Participant AI models execute in isolated, sandboxed environments with zero network connectivity and bounded resource allocations.

---

## 2. Competition Structure

### 2.1 Event Phases
1. **Registration & Check-In:**
   - Participants register and receive unique server-issued credentials (`Participant ID` and one-time `Access Key`).
   - The authoritative build clock begins upon account creation.
2. **Build & Iteration Window:**
   - Participants build and test their models in their own local development environments.
   - Submissions may be iteratively uploaded to the validation pipeline.
   - Static validation verifies package integrity, entrypoint compliance, and file quotas.
   - Reviewers examine validated submissions to grant official competition eligibility.
   - Participants test eligible models in development matches against the organizer House AI.
3. **Official Submission Selection & Freeze:**
   - Each participant selects one eligible submission version as their intended official tournament entry.
   - At the designated freeze time, the tournament director atomically triggers an official freeze via `/api/v1/admin/freeze`. All official submission selections become permanently immutable.
4. **Official Knockout Tournament:**
   - Single-elimination tournament bracket populated exclusively from frozen official submissions.
   - Official matches executed sequentially or concurrently across the secure worker fleet.
   - Advancing winners proceed to the next round; the final winner is crowned AI Knockout Champion.

---

## 3. Chess Rules and Match Parameters

### 3.1 Rules of Play
- Standard Chess rules as defined by FIDE, governed by `python-chess 1.11.2`.
- Standard starting position FEN: `rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1`
- Move notation: Standard UCI (Universal Chess Interface) coordinate notation (e.g. `e2e4`, `g1f3`, `e7e8q` for promotion).

### 3.2 Time Controls & Clocks
- **Match Time (Initial Clock):** 300,000 milliseconds (5 minutes) per participant.
- **Clock Increment:** 0 milliseconds per move.
- **Move Deadline (Per-Ply Timeout):** 2,000 milliseconds (2.0 seconds) maximum wall-clock time per ply.
- The server tracks remaining time to millisecond precision. Time spent thinking is subtracted from the active player's remaining clock.

### 3.3 Match Outcomes & Point Scoring
- **Win (1.0 Point):**
  - Checkmate on the board.
  - Opponent clock expiration (`MOVE_TIMEOUT` or remaining clock reaches 0).
  - Opponent illegal move submission (`ILLEGAL_MOVE`).
  - Opponent process crash, exit code non-zero, or unhandled exception (`PROCESS_CRASH`).
  - Opponent resource quota violation (`RESOURCE_EXCEEDED`).
- **Draw (0.5 Points):**
  - Stalemate.
  - Threefold repetition / Fivefold repetition.
  - Fifty-move rule / Seventy-five move rule.
  - Insufficient material (Dead position).
- **Loss (0.0 Points):**
  - The side that was checkmated, timed out, crashed, or committed a foul.

### 3.4 Tie-Breakers (Official Tournament Standings)
When ranking participants across tournament rounds:
1. **Total Match Points:** Sum of points scored in official matches.
2. **Sonneborn-Berger Score:** Sum of the final scores of opponents defeated, plus half the final scores of opponents drawn against.
3. **Head-to-Head Result:** Result of the direct match between tied participants (if applicable).
4. **Playoff Match:** If a playoff is required for bracket advancement, an additional official tie-breaker match is scheduled by the tournament director.

---

## 4. Participant Submission Specification

### 4.1 Packaging Requirements
- **Archive Format:** Uncompressed USTAR POSIX `.tar` archive. (Compressed formats like `.tar.gz`, `.zip`, `.7z` are strictly rejected by the validator with status 415/422).
- **Maximum Archive Size:** 10,485,760 bytes (10 MB).
- **Maximum File Count:** 100 files per archive.
- **Maximum Single File Size:** 5,242,880 bytes (5 MB).
- **Filename Security:** All file paths must be relative and safe. Path traversal sequences (`../`, leading slashes `/`, absolute Windows drive letters `C:`) cause immediate quarantine rejection.

### 4.2 Required Directory Layout
The root of the archive must contain the entrypoint file:
```
submission.tar
├── entrypoint.py       (Primary entrypoint; alternatively main.py)
├── weights.json        (Optional model weights or opening book)
└── helper_module/      (Optional support Python files)
    ├── __init__.py
    └── evaluation.py
```

### 4.3 Runtime Environment
- **Runtime:** Python 3.12 (standard CPython).
- **Pre-installed Libraries in Worker Container:**
  - `python-chess >= 1.11.0`
  - `numpy >= 1.26.0`
  - Standard Python standard library (`math`, `random`, `json`, `sys`, `time`).
- **Forbidden Operations:**
  - Network calls (`socket`, `urllib`, `requests`, `http.client`) — blocked at sandbox kernel level.
  - Subprocess spawning (`subprocess`, `os.system`, `multiprocessing`).
  - Filesystem writes outside `/tmp` (ephemeral tmpfs with 32MB limit).
  - Malicious calls attempting container breakout or privilege escalation.

### 4.4 Communication Protocol (`aik-jsonl/1`)
The participant AI communicates with the match controller via standard input (`stdin`) and standard output (`stdout`) using JSON Lines (one JSON object per line terminated with `\n`):

#### 1. Initialization Request (Controller -> Participant Engine):
Sent once when the match engine launches:
```json
{"type": "init", "color": "white", "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "clock_ms": 300000}
```
**Engine Response (Participant -> Controller):**
```json
{"type": "ready", "name": "DeepKnockout", "version": "1.0.0"}
```

#### 2. Move Request (Controller -> Participant Engine):
Sent on each turn when it is the participant's turn to move:
```json
{"type": "move_request", "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1", "remaining_ms": 298450, "move_timeout_ms": 2000, "ply": 1}
```
**Engine Response (Participant -> Controller):**
Must respond within `move_timeout_ms` (2.0s):
```json
{"type": "move", "uci": "e7e5"}
```

#### 3. Match End Notification (Controller -> Participant Engine):
Sent when the match concludes:
```json
{"type": "game_over", "outcome": "WHITE_WIN", "reason": "CHECKMATE"}
```

### 4.5 Minimal Compliant Sample Agent
Participants may use this template as a starting point for their submission:

```python
#!/usr/bin/env python3
"""AI Knockout Reference Sample Agent (Python 3.12)."""
import json
import sys
import chess

def send_response(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

def main():
    board = chess.Board()
    my_color = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")

        if msg_type == "init":
            my_color = msg.get("color")
            initial_fen = msg.get("initial_fen", chess.STARTING_FEN)
            board.set_fen(initial_fen)
            send_response({"type": "ready", "name": "ReferenceBot", "version": "1.0"})

        elif msg_type == "move_request":
            fen = msg.get("fen")
            if fen:
                board.set_fen(fen)
            
            # Simple fallback: Pick first legal move (or implement minimax / evaluation)
            legal_moves = list(board.legal_moves)
            if legal_moves:
                chosen_move = legal_moves[0].uci()
            else:
                chosen_move = "0000"  # Resign / null move

            send_response({"type": "move", "uci": chosen_move})

        elif msg_type == "game_over":
            break

if __name__ == "__main__":
    main()
```
