# AI Knockout — Registration Support and Dispute Procedures
**Event:** AI Knockout — MVSR Engineering College  
**Target Audience:** Participant Support Desk, Technical Volunteers, Tournament Referee  
**Document Version:** 1.0  
**Status:** Operational Protocol

---

## 1. Registration & Participant Support

### 1.1 Standard Registration Flow
1. Participant navigates to the event portal at `http://<SERVER_HOST>/`.
2. Participant clicks "Register" and inputs:
   - AI / Entry Name (Required, e.g. `QuantumKnight`)
   - Team Name (Optional)
   - Contact Email (Optional)
3. The server responds with:
   - `Participant ID`: e.g. `part_a8f9b201`
   - `Access Key`: e.g. `sec_8f7b...` (displayed exactly once)
4. The participant MUST copy and store both credentials immediately. The Access Key is hashed with argon2id / PBKDF2 on the server and cannot be retrieved again.

### 1.2 Lost Credentials Protocol
If a participant loses their Access Key before submitting code:
- **Scenario A: Prior to Official Submissions & Freeze:**
  - The participant should register a new account (e.g. `Team_Beta_2`).
  - The support desk notes the duplicate identity and flags the old ID for deactivation.
- **Scenario B: After Submitting Code:**
  - The participant must physically approach the Help Desk with student ID card / college registration badge.
  - The Lead Systems Engineer can generate an emergency session token or issue a replacement key via audited administrative script:
    `python scripts/admin_reissue_access.py --participant-id <PID> --authorized-by <DESK_LEAD>`
  - The action is recorded in `admin_audit_log` with timestamp, student name, and reason.

---

## 2. Submission Validation Troubleshooting

When a participant receives an upload rejection (Status 415 or 422), consult the following troubleshooting table:

| Error Message / Code | Root Cause | Resolution |
| :--- | :--- | :--- |
| `Upload an uncompressed USTAR .tar archive.` (415) | Participant uploaded a `.zip`, `.tar.gz`, `.tar.bz2`, or compressed archive. | Create an uncompressed tar archive: `tar -cf submission.tar entrypoint.py` |
| `Archive exceeds server upload limit.` (413) | Archive size exceeds 10 MB limit (often caused by large pre-trained neural net weights). | Prune unused libraries or reduce model size to < 10 MB. |
| `Missing required entrypoint.` (422) | Archive does not contain `entrypoint.py` or `main.py` at the root level. | Move script to root of archive. Avoid nesting inside subdirectories like `src/entrypoint.py`. |
| `Invalid path traversal in archive.` (422) | Archive contains relative path traversal (`../`) or absolute paths (`/etc/`, `C:\`). | Package files using relative names: `cd my_agent && tar -cf ../submission.tar *` |
| `SyntaxError during static parsing.` (422) | Python code in entrypoint contains syntax errors. | Test agent locally with `python3 -m py_compile entrypoint.py` before uploading. |

---

## 3. Match Dispute Handling & Evidence Verification

### 3.1 Dispute Eligibility
Disputes must be raised by the registered team lead within **10 minutes** of match conclusion. Disputes are only accepted on the following grounds:
1. Server crash or infrastructure disruption during match execution.
2. Contested illegal move or false forfeit claim.
3. Clock synchronization discrepancy.

### 3.2 Server-Authoritative Verification Procedure
The Tournament Director and Lead Systems Engineer follow this four-step forensic protocol:

#### Step 1: Query Match Record & State
Retrieve the full official match record via API:
```bash
curl -H "X-Organizer-Secret: $ADMIN_SECRET" \
  http://localhost:8000/api/v1/tournaments/matches/<MATCH_ID>
```
Verify the recorded `result_outcome` (`WHITE_WIN`, `BLACK_WIN`, `DRAW`) and `result_reason` (`CHECKMATE`, `STALEMATE`, `MOVE_TIMEOUT`, `ILLEGAL_MOVE`, etc.).

#### Step 2: Extract Cryptographic Move Transcript
The coordinator persists every ply with:
- Move notation in UCI (e.g. `e2e4`)
- Pre-move and post-move FEN string
- Elapsed execution time (ms)
- Remaining white and black clock (ms)
- Cryptographic hash of the match state

#### Step 3: Replay Validation
Run the official offline replay validator to confirm whether the move sequence was strictly legal according to `python-chess 1.11.2`:
```bash
python scripts/verify_match_transcript.py --match-id <MATCH_ID>
```

#### Step 4: Final Ruling
- If the transcript proves an illegal move or timeout occurred as recorded, the server outcome is **upheld as final**.
- If a verified host worker hardware/hypervisor crash disrupted the container communication before the move deadline expired, the Tournament Director may order an immediate rematch with identical seeds and colors.
- The referee's decision based on the verified server transcript is final and binding.
