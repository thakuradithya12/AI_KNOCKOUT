import { api, ApiError } from "/assets/api.js?v=ui-20261015-02";
import { credentialPasteValues, credentialsText, copyCredentialValue } from "/assets/credentials.js?v=ui-20261015-02";
import { resolveQuotaSnapshot, resolveSubmissionSnapshot } from "/assets/submissionState.js?v=ui-20261015-02";
import { renderOrganizerMatchManagement, renderOrganizerRefreshSnapshot, wireOrganizerMatchManagement } from "/assets/organizerMatchManagement.js?v=ui-20261015-02";
import { bindArchiveDrop } from "/assets/uploadDrop.js?v=ui-20261015-02";
import { resultSourceLabel } from "/assets/resultProvenance.js?v=ui-20261015-02";

const app = document.querySelector("#app");
const toast = document.querySelector("#toast");

// The existing backend issues opaque bearer tokens. Keep the token in memory
// only; a page reload intentionally requires the participant to sign in again.
let sessionToken = null;
let activeParticipant = null;
let organizerCsrfToken = null;
let organizerProfile = null;
let route = "login";
// Keep participant-code match execution unavailable until its sandbox is verified.
const PARTICIPANT_CODE_EXECUTION_ENABLED = false;
let toastTimer;
let arenaRefreshTimer = null;
let arenaCurrentRefreshSec = 15;
let arenaLiveRequestPending = false;
let arenaAutoPlayTimer = null;
let matchDetailTimer = null;

function stopArenaRefresh() {
  if (arenaRefreshTimer) {
    clearTimeout(arenaRefreshTimer);
    clearInterval(arenaRefreshTimer);
    arenaRefreshTimer = null;
  }
  if (arenaAutoPlayTimer) {
    clearInterval(arenaAutoPlayTimer);
    arenaAutoPlayTimer = null;
  }
  if (matchDetailTimer) {
    clearTimeout(matchDetailTimer);
    clearInterval(matchDetailTimer);
    matchDetailTimer = null;
  }
}

function notify(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2300);
}

function esc(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function themeToggleMarkup() {
  const dark = window.AIKTheme?.current() === "dark";
  return `<button class="theme-toggle" type="button" data-theme-toggle aria-label="Switch to ${dark ? "light" : "dark"} theme" aria-pressed="${dark}" title="Switch to ${dark ? "light" : "dark"} theme"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${dark ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>' : '<path d="M20.5 15.5A8.5 8.5 0 0 1 8.5 3.5 8.5 8.5 0 1 0 20.5 15.5Z"/>'}</svg><span>${dark ? "Dark" : "Light"}</span></button>`;
}

app.addEventListener("click", (event) => {
  if (!(event.target instanceof Element) || !event.target.closest("[data-theme-toggle]") || !window.AIKTheme?.toggle()) return;
  app.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    const dark = window.AIKTheme.current() === "dark";
    button.setAttribute("aria-pressed", String(dark));
    button.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} theme`);
    button.title = `Switch to ${dark ? "light" : "dark"} theme`;
    button.querySelector("span").textContent = dark ? "Dark" : "Light";
    button.querySelector("svg").innerHTML = dark
      ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>'
      : '<path d="M20.5 15.5A8.5 8.5 0 0 1 8.5 3.5 8.5 8.5 0 1 0 20.5 15.5Z"/>';
  });
});

const INITIAL_POSITION_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

// Both colours use the solid glyph shapes; colour comes from CSS. The hollow white
// glyphs (♙♖…) almost vanish on light squares. U+FE0E keeps the text (non-emoji) form.
const PIECE_GLYPHS = Object.freeze(Object.fromEntries(
  Object.entries({ p: "♟", r: "♜", n: "♞", b: "♝", q: "♛", k: "♚" })
    .flatMap(([key, glyph]) => [[key, `${glyph}\uFE0E`], [key.toUpperCase(), `${glyph}\uFE0E`]]),
));
const PIECE_NAMES = Object.freeze({ p: "pawn", r: "rook", n: "knight", b: "bishop", q: "queen", k: "king" });

function pieceAriaLabel(square, piece) {
  if (!piece) return `${square} empty`;
  const colour = piece === piece.toUpperCase() ? "White" : "Black";
  return `${square} ${colour} ${PIECE_NAMES[piece.toLowerCase()] || "piece"}`;
}

function parseFenRows(fen) {
  if (typeof fen !== "string" || !fen.trim()) return null;
  const placement = fen.trim().split(" ")[0];
  const rankStrs = placement.split("/");
  if (rankStrs.length !== 8) return null;
  const rows = rankStrs.map((rankStr) => {
    let row = "";
    for (const ch of rankStr) {
      if (ch >= "1" && ch <= "8") {
        row += ".".repeat(Number(ch));
      } else if (/[prnbqkPRNBQK]/.test(ch)) {
        row += ch;
      } else return null;
    }
    return row.length === 8 ? row : null;
  });
  return rows.every(Boolean) ? rows : null;
}

function detectKingInCheck(fen) {
  if (typeof fen !== "string" || !fen.trim()) return null;
  const parts = fen.trim().split(/\s+/);
  const rows = parseFenRows(parts[0]);
  if (!rows) return null;
  const turn = parts[1] || "w";
  const kingChar = turn === "w" ? "K" : "k";
  let kingRow = -1;
  let kingCol = -1;
  for (let r = 0; r < 8; r += 1) {
    for (let c = 0; c < 8; c += 1) {
      if (rows[r][c] === kingChar) {
        kingRow = r;
        kingCol = c;
        break;
      }
    }
    if (kingRow !== -1) break;
  }
  if (kingRow === -1) return null;

  const files = ["a", "b", "c", "d", "e", "f", "g", "h"];
  const ranks = ["8", "7", "6", "5", "4", "3", "2", "1"];
  const kingSq = files[kingCol] + ranks[kingRow];

  const enemyKnight = turn === "w" ? "n" : "N";
  const enemyPawn = turn === "w" ? "p" : "P";
  const enemyBishop = turn === "w" ? "b" : "B";
  const enemyRook = turn === "w" ? "r" : "R";
  const enemyQueen = turn === "w" ? "q" : "Q";

  const knightDeltas = [[-2, -1], [-2, 1], [-1, -2], [-1, 2], [1, -2], [1, 2], [2, -1], [2, 1]];
  for (const [dr, dc] of knightDeltas) {
    const nr = kingRow + dr;
    const nc = kingCol + dc;
    if (nr >= 0 && nr < 8 && nc >= 0 && nc < 8 && rows[nr][nc] === enemyKnight) return kingSq;
  }

  const pawnRow = turn === "w" ? kingRow - 1 : kingRow + 1;
  if (pawnRow >= 0 && pawnRow < 8) {
    if (kingCol - 1 >= 0 && rows[pawnRow][kingCol - 1] === enemyPawn) return kingSq;
    if (kingCol + 1 < 8 && rows[pawnRow][kingCol + 1] === enemyPawn) return kingSq;
  }

  const orthoDirs = [[-1, 0], [1, 0], [0, -1], [0, 1]];
  for (const [dr, dc] of orthoDirs) {
    let nr = kingRow + dr;
    let nc = kingCol + dc;
    while (nr >= 0 && nr < 8 && nc >= 0 && nc < 8) {
      const p = rows[nr][nc];
      if (p !== ".") {
        if (p === enemyRook || p === enemyQueen) return kingSq;
        break;
      }
      nr += dr;
      nc += dc;
    }
  }

  const diagDirs = [[-1, -1], [-1, 1], [1, -1], [1, 1]];
  for (const [dr, dc] of diagDirs) {
    let nr = kingRow + dr;
    let nc = kingCol + dc;
    while (nr >= 0 && nr < 8 && nc >= 0 && nc < 8) {
      const p = rows[nr][nc];
      if (p !== ".") {
        if (p === enemyBishop || p === enemyQueen) return kingSq;
        break;
      }
      nr += dr;
      nc += dc;
    }
  }

  return null;
}

function calculateMaterialBalance(fen) {
  if (typeof fen !== "string" || !fen.trim()) {
    return { whiteCaptured: [], blackCaptured: [], whiteScore: 0, blackScore: 0, diff: 0 };
  }
  const parts = fen.trim().split(/\s+/);
  const rows = parseFenRows(parts[0]);
  if (!rows) return { whiteCaptured: [], blackCaptured: [], whiteScore: 0, blackScore: 0, diff: 0 };
  const values = { p: 1, n: 3, b: 3, r: 5, q: 9 };
  const glyphs = { p: "♟", n: "♞", b: "♝", r: "♜", q: "♛", P: "♙", N: "♘", B: "♗", R: "♖", Q: "♕" };
  const initialBlack = { p: 8, n: 2, b: 2, r: 2, q: 1 };
  const initialWhite = { P: 8, N: 2, B: 2, R: 2, Q: 1 };
  const curBlack = { p: 0, n: 0, b: 0, r: 0, q: 0 };
  const curWhite = { P: 0, N: 0, B: 0, R: 0, Q: 0 };

  for (let r = 0; r < 8; r += 1) {
    for (let c = 0; c < 8; c += 1) {
      const ch = rows[r][c];
      if (ch in curBlack) curBlack[ch] += 1;
      else if (ch in curWhite) curWhite[ch] += 1;
    }
  }

  const whiteCaptured = [];
  let whiteScore = 0;
  for (const [k, count] of Object.entries(initialBlack)) {
    const diff = Math.max(0, count - curBlack[k]);
    for (let i = 0; i < diff; i += 1) {
      whiteCaptured.push(glyphs[k]);
      whiteScore += values[k];
    }
  }

  const blackCaptured = [];
  let blackScore = 0;
  for (const [k, count] of Object.entries(initialWhite)) {
    const diff = Math.max(0, count - curWhite[k]);
    for (let i = 0; i < diff; i += 1) {
      blackCaptured.push(glyphs[k]);
      blackScore += values[k.toLowerCase()];
    }
  }

  return {
    whiteCaptured,
    blackCaptured,
    whiteScore,
    blackScore,
    diff: whiteScore - blackScore,
  };
}

function renderCinematicBoard(fen, options = {}) {
  const { from = null, to = null, boardId = "", flipped = false, checkSquare = null } = options;
  const glyphs = PIECE_GLYPHS;
  const rows = parseFenRows(fen);
  if (!rows) return `<div class="board-unavailable" role="status">Server position could not be displayed.</div>`;
  const files = ["a", "b", "c", "d", "e", "f", "g", "h"];
  const ranks = ["8", "7", "6", "5", "4", "3", "2", "1"];

  const displayFiles = flipped ? [...files].reverse() : files;
  const displayRanks = flipped ? [...ranks].reverse() : ranks;
  const inCheckSq = checkSquare || detectKingInCheck(fen);

  let squaresHtml = "";
  for (let r = 0; r < 8; r += 1) {
    const rank = displayRanks[r];
    const origRow = 8 - parseInt(rank, 10);
    for (let f = 0; f < 8; f += 1) {
      const file = displayFiles[f];
      const origCol = file.charCodeAt(0) - 97;
      const sq = file + rank;
      const isDark = (origRow + origCol) % 2 === 1;
      const piece = rows[origRow][origCol] !== "." ? rows[origRow][origCol] : "";
      const isFrom = from === sq;
      const isTo = to === sq;
      const isCheck = inCheckSq === sq;
      let hlClass = "";
      if (isFrom) hlClass += " hl-from";
      if (isTo) hlClass += " hl-to";
      if (isCheck) hlClass += " in-check";

      const glyph = glyphs[piece] || "";
      const isWhite = piece && piece === piece.toUpperCase();
      const pieceColorClass = piece ? (isWhite ? " piece-white" : " piece-black") : "";
      squaresHtml += `<span class="square ${isDark ? "dark" : "light"}${hlClass}" data-sq="${sq}" aria-label="${pieceAriaLabel(sq, piece)}">${glyph ? `<span class="piece${pieceColorClass}">${glyph}</span>` : ""}</span>`;
    }
  }

  const fileTrack = displayFiles.map((file) => `<span>${file}</span>`).join("");
  const rankTrack = displayRanks.map((rank) => `<span>${rank}</span>`).join("");

  return `<div class="cinematic-board-bezel"${boardId ? ` id="${esc(boardId)}"` : ""} data-flipped="${Boolean(flipped)}">
    <div class="coord-track coord-top" aria-hidden="true">${fileTrack}</div>
    <div class="board-core-row">
      <div class="coord-track coord-left" aria-hidden="true">${rankTrack}</div>
      <div class="cinematic-board-grid" role="img" aria-label="Chessboard position">${squaresHtml}</div>
      <div class="coord-track coord-right" aria-hidden="true">${rankTrack}</div>
    </div>
    <div class="coord-track coord-bottom" aria-hidden="true">${fileTrack}</div>
  </div>`;
}

function updateCinematicBoard(container, fen, options = {}) {
  if (!container) return;
  const { from = null, to = null, flipped = false, checkSquare = null } = options;
  const bezel = container.querySelector(".cinematic-board-bezel");
  const grid = container.querySelector(".cinematic-board-grid");
  const currentFlipped = bezel ? bezel.dataset.flipped === "true" : false;

  if (!grid || currentFlipped !== Boolean(flipped)) {
    container.innerHTML = renderCinematicBoard(fen, { from, to, flipped, checkSquare });
    return;
  }

  const rows = parseFenRows(fen);
  if (!rows) return;
  const glyphs = PIECE_GLYPHS;
  const inCheckSq = checkSquare || detectKingInCheck(fen);

  const squareEls = grid.querySelectorAll(".square[data-sq]");
  for (const sqEl of squareEls) {
    const sq = sqEl.dataset.sq;
    const file = sq[0];
    const rank = sq[1];
    const colIdx = file.charCodeAt(0) - 97;
    const rowIdx = 8 - parseInt(rank, 10);
    const piece = rows[rowIdx][colIdx] !== "." ? rows[rowIdx][colIdx] : "";
    const glyph = glyphs[piece] || "";
    const isWhite = piece && piece === piece.toUpperCase();
    const pieceColorClass = piece ? (isWhite ? " piece-white" : " piece-black") : "";

    const isFrom = from === sq;
    const isTo = to === sq;
    const isCheck = inCheckSq === sq;

    sqEl.classList.toggle("hl-from", isFrom);
    sqEl.classList.toggle("hl-to", isTo);
    sqEl.classList.toggle("in-check", isCheck);

    const existingPiece = sqEl.querySelector(".piece");
    if (!piece) {
      if (existingPiece) existingPiece.remove();
      sqEl.setAttribute("aria-label", pieceAriaLabel(sq, piece));
    } else {
      sqEl.setAttribute("aria-label", pieceAriaLabel(sq, piece));
      if (!existingPiece) {
        sqEl.innerHTML = `<span class="piece${pieceColorClass}">${glyph}</span>`;
      } else {
        if (existingPiece.textContent !== glyph || !existingPiece.classList.contains(pieceColorClass.trim())) {
          existingPiece.className = `piece${pieceColorClass}`;
          existingPiece.textContent = glyph;
        }
      }
    }
  }
}

let landingAnimationTimer = null;
let landingAnimationPly = 0;

const OPENING_ANIMATION_SEQUENCE = [
  { fen: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1", from: "e2", to: "e4", moveText: "1. e4", note: "King's Pawn opening" },
  { fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2", from: "e7", to: "e5", moveText: "1... e5", note: "Open Game" },
  { fen: "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2", from: "g1", to: "f3", moveText: "2. Nf3", note: "King's Knight develops" },
  { fen: "rnbqkbnr/ppp2ppp/3p4/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3", from: "d7", to: "d6", moveText: "2... d6", note: "Philidor Defence" },
  { fen: "rnbqkbnr/ppp2ppp/3p4/4p3/3PP3/5N2/PPP2PPP/RNBQKB1R b KQkq d3 0 3", from: "d2", to: "d4", moveText: "3. d4", note: "Center strike" },
  { fen: "rn1qkbnr/ppp2ppp/3p4/4p3/3PP1b1/5N2/PPP2PPP/RNBQKB1R w KQkq - 1 4", from: "c8", to: "g4", moveText: "3... Bg4", note: "Pin on knight" },
  { fen: "rn1qkbnr/ppp2ppp/3p4/4P3/4P1b1/5N2/PPP2PPP/RNBQKB1R b KQkq - 0 4", from: "d4", to: "e5", moveText: "4. dxe5", note: "Center opened" },
  { fen: "rn1qkbnr/ppp2ppp/3p4/4P3/4P3/5b2/PPP2PPP/RNBQKB1R w KQkq - 0 5", from: "g4", to: "f3", moveText: "4... Bxf3", note: "Bishop exchange" },
  { fen: "rn1qkbnr/ppp2ppp/3p4/4P3/4P3/5Q2/PPP2PPP/RNB1KB1R b KQkq - 0 5", from: "d1", to: "f3", moveText: "5. Qxf3", note: "Queen recaptures" },
  { fen: "rn1qkbnr/ppp2ppp/8/4p3/4P3/5Q2/PPP2PPP/RNB1KB1R w KQkq - 0 6", from: "d6", to: "e5", moveText: "5... dxe5", note: "Pawn recaptured" },
  { fen: "rn1qkbnr/ppp2ppp/8/4p3/2B1P3/5Q2/PPP2PPP/RNB1K2R b KQkq - 1 6", from: "f1", to: "c4", moveText: "6. Bc4", note: "Targeting f7" },
  { fen: "rn1qkb1r/ppp2ppp/5n2/4p3/2B1P3/5Q2/PPP2PPP/RNB1K2R w KQkq - 2 7", from: "g8", to: "f6", moveText: "6... Nf6", note: "Defending f7" },
  { fen: "rn1qkb1r/ppp2ppp/5n2/4p3/2B1P3/1Q6/PPP2PPP/RNB1K2R b KQkq - 3 7", from: "f3", to: "b3", moveText: "7. Qb3", note: "Double threat b7 & f7" },
  { fen: "rn2kb1r/ppp1qppp/5n2/4p3/2B1P3/1Q6/PPP2PPP/RNB1K2R w KQkq - 4 8", from: "d8", to: "e7", moveText: "7... Qe7", note: "Queen guards f7" },
  { fen: "rn2kb1r/ppp1qppp/5n2/4p3/2B1P3/1QN5/PPP2PPP/R1B1K2R b KQkq - 5 8", from: "b1", to: "c3", moveText: "8. Nc3", note: "Piece mobilization" },
  { fen: "rn2kb1r/pp2qppp/2p2n2/4p3/2B1P3/1QN5/PPP2PPP/R1B1K2R w KQkq - 0 9", from: "c7", to: "c6", moveText: "8... c6", note: "Guarding b5" },
  { fen: "rn2kb1r/pp2qppp/2p2n2/4p1B1/2B1P3/1QN5/PPP2PPP/R1B1K2R b KQkq - 1 9", from: "c1", to: "g5", moveText: "9. Bg5", note: "Pin on knight" },
  { fen: "rn2kb1r/p3qppp/2p2n2/1p2p1B1/2B1P3/1QN5/PPP2PPP/R1B1K2R w KQkq b6 0 10", from: "b7", to: "b5", moveText: "9... b5", note: "Counterattack" },
  { fen: "rn2kb1r/p3qppp/2p2n2/1N2p1B1/2B1P3/1Q6/PPP2PPP/R1B1K2R b KQkq - 0 10", from: "c3", to: "b5", moveText: "10. Nxb5", note: "Knight sacrifice" },
  { fen: "rn2kb1r/p3qppp/5n2/1p2p1B1/2B1P3/1Q6/PPP2PPP/R1B1K2R w KQkq - 0 11", from: "c6", to: "b5", moveText: "10... cxb5", note: "Sacrifice accepted" },
  { fen: "rn2kb1r/p3qppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/R1B1K2R b KQkq - 0 11", from: "c4", to: "b5", moveText: "11. Bxb5+", note: "Check!" },
  { fen: "r3kb1r/p2nqppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/R1B1K2R w KQkq - 1 12", from: "b8", to: "d7", moveText: "11... Nbd7", note: "Interposed" },
  { fen: "r3kb1r/p2nqppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/2KR3R b kq - 2 12", from: "e1", to: "c1", moveText: "12. O-O-O", note: "Queenside Castling", castleRook: { from: "a1", to: "d1" } },
  { fen: "3rkb1r/p2nqppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/2KR3R w k - 3 13", from: "a8", to: "d8", moveText: "12... Rd8", note: "Rook defends" },
  { fen: "3rkb1r/p2Rqppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/2K4R b k - 0 13", from: "d1", to: "d7", moveText: "13. Rxd7", note: "Exchange sacrifice" },
  { fen: "4kb1r/p2rqppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/2K4R w k - 0 14", from: "d8", to: "d7", moveText: "13... Rxd7", note: "Recaptured" },
  { fen: "4kb1r/p2rqppp/5n2/1B2p1B1/4P3/1Q6/PPP2PPP/2KR4 b k - 1 14", from: "h1", to: "d1", moveText: "14. Rd1", note: "Doubled rooks on pin" },
  { fen: "4kb1r/p2r1ppp/4qn2/1B2p1B1/4P3/1Q6/PPP2PPP/2KR4 w k - 2 15", from: "e7", to: "e6", moveText: "14... Qe6", note: "Queen unpins" },
  { fen: "4kb1r/p2B1ppp/4qn2/4p1B1/4P3/1Q6/PPP2PPP/2KR4 b k - 0 15", from: "b5", to: "d7", moveText: "15. Bxd7+", note: "Decisive clearance" },
  { fen: "4kb1r/p2n1ppp/4q3/4p1B1/4P3/1Q6/PPP2PPP/2KR4 w k - 0 16", from: "f6", to: "d7", moveText: "15... Nxd7", note: "Knight recaptures" },
  { fen: "1Q2kb1r/p2n1ppp/4q3/4p1B1/4P3/8/PPP2PPP/2KR4 b k - 1 16", from: "b3", to: "b8", moveText: "16. Qb8+", note: "Queen sacrifice check!" },
  { fen: "1n2kb1r/p4ppp/4q3/4p1B1/4P3/8/PPP2PPP/2KR4 w k - 0 17", from: "d7", to: "b8", moveText: "16... Nxb8", note: "Forced capture" },
  { fen: "1n1Rkb1r/p4ppp/4q3/4p1B1/4P3/8/PPP2PPP/2K5 b k - 1 17", from: "d1", to: "d8", moveText: "17. Rd8#", note: "Checkmate! White wins 1-0" },
];

function isReducedMotionPreferred() {
  return typeof window !== "undefined" && Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function stopLandingAnimation() {
  if (landingAnimationTimer) {
    clearTimeout(landingAnimationTimer);
    landingAnimationTimer = null;
  }
}

function startLandingAnimation() {
  stopLandingAnimation();
  if (isReducedMotionPreferred()) return;
  landingAnimationPly = 0;
  scheduleNextLandingMove(2200);
}

function scheduleNextLandingMove(delayMs = 3200) {
  stopLandingAnimation();
  if (isReducedMotionPreferred()) return;
  landingAnimationTimer = setTimeout(stepLandingAnimation, delayMs);
}

function stepLandingAnimation() {
  if (isReducedMotionPreferred() || (typeof document !== "undefined" && document.hidden)) {
    scheduleNextLandingMove(3200);
    return;
  }
  const container = app.querySelector("#public-demo-board-container");
  const caption = app.querySelector("#access-board-caption");
  if (!container || !container.isConnected) {
    stopLandingAnimation();
    return;
  }

  if (landingAnimationPly >= OPENING_ANIMATION_SEQUENCE.length) {
    landingAnimationPly = 0;
    container.innerHTML = renderCinematicBoard(INITIAL_POSITION_FEN);
    if (caption) caption.innerHTML = `<span>Starting Position &middot; Illustrative opening moves</span>`;
    scheduleNextLandingMove(3200);
    return;
  }

  const step = OPENING_ANIMATION_SEQUENCE[landingAnimationPly];
  landingAnimationPly += 1;

  const fromSquare = container.querySelector(`[data-sq="${step.from}"]`);
  const pieceSpan = fromSquare ? fromSquare.querySelector(".piece") : null;

  if (pieceSpan) {
    const fromCol = step.from.charCodeAt(0) - 97;
    const fromRow = 8 - parseInt(step.from[1], 10);
    const toCol = step.to.charCodeAt(0) - 97;
    const toRow = 8 - parseInt(step.to[1], 10);
    const dxPercent = (toCol - fromCol) * 100;
    const dyPercent = (toRow - fromRow) * 100;

    pieceSpan.style.transition = "transform 620ms cubic-bezier(0.2, 0.8, 0.25, 1)";
    pieceSpan.style.transform = `translate(${dxPercent}%, ${dyPercent}%)`;
    pieceSpan.style.zIndex = "10";
    fromSquare.classList.add("hl-from");

    if (step.castleRook) {
      const rookSquare = container.querySelector(`[data-sq="${step.castleRook.from}"]`);
      const rookPiece = rookSquare ? rookSquare.querySelector(".piece") : null;
      if (rookPiece) {
        const rFromCol = step.castleRook.from.charCodeAt(0) - 97;
        const rToCol = step.castleRook.to.charCodeAt(0) - 97;
        const rDx = (rToCol - rFromCol) * 100;
        rookPiece.style.transition = "transform 620ms cubic-bezier(0.2, 0.8, 0.25, 1)";
        rookPiece.style.transform = `translate(${rDx}%, 0%)`;
        rookPiece.style.zIndex = "9";
      }
    }

    setTimeout(() => {
      if (!container.isConnected) return;
      container.innerHTML = renderCinematicBoard(step.fen, { from: step.from, to: step.to });
      if (caption) caption.innerHTML = `<span>${esc(step.moveText)} &middot; ${esc(step.note)}</span>`;
      scheduleNextLandingMove(3200);
    }, 640);
  } else {
    container.innerHTML = renderCinematicBoard(step.fen, { from: step.from, to: step.to });
    if (caption) caption.innerHTML = `<span>${esc(step.moveText)} &middot; ${esc(step.note)}</span>`;
    scheduleNextLandingMove(3200);
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopLandingAnimation();
    } else if (route === "login" || route === "register") {
      scheduleNextLandingMove(1200);
    }
  });
}

function landing(active = "login", error = "", initialValues = {}) {
  stopLandingAnimation();
  stopArenaRefresh();
  stopOverviewPolling();
  document.body.classList.remove("public-interface");
  document.body.classList.add("entry-page");
  route = "login";
  activeParticipant = null;

  app.innerHTML = `
    <section class="access-layout" aria-label="Participant access">
      <div class="access-context">
        <div class="access-toolbar"><div class="section-index">AI KNOCKOUT <span>2026</span></div></div>
        <h1>Participant Sign In</h1>
        <p class="context-copy">Use the participant credentials issued by tournament organizers to access your workspace.</p>
        <dl class="event-facts">
          <div><dt>Event</dt><dd>2026 Tournament</dd></div>
          <div><dt>Discipline</dt><dd>Autonomous Chess</dd></div>
          <div><dt>Access</dt><dd>Organizer Issued</dd></div>
        </dl>
        <nav class="access-views" aria-label="Public navigation">
          <button class="access-nav-btn active" type="button" data-view="participant" aria-current="page">Participant</button>
          <button class="access-nav-btn" type="button" data-view="admin">Organizer</button>
          <button class="access-nav-btn" type="button" data-view="arena">Arena</button>
        </nav>
      </div>
      <figure class="access-board" aria-labelledby="access-board-caption">
        <div class="board-label"><span>CHESS STUDY</span><span class="board-coordinates">STAGE</span></div>
        <div id="public-demo-board-container" class="cinematic-board-stage">
          ${renderCinematicBoard(INITIAL_POSITION_FEN)}
        </div>
        <figcaption id="access-board-caption">
          <span>Starting Position &middot; Illustrative master game</span>
        </figcaption>
      </figure>
      <section class="access-panel" aria-label="Participant access panel">
        ${error ? `
          <div class="error" role="alert">
            ${esc(error)}
          </div>
        ` : ""}
        <div id="panel-login" role="region" aria-label="Participant credentials">
          ${loginForm(initialValues)}
        </div>
      </section>
    </section>`;

  app.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => renderPublicView(button.dataset.view)));

  const logForm = app.querySelector("#login-form");
  if (logForm) {
    logForm.addEventListener("submit", login);
    bindCredentialPaste();
  }

  startLandingAnimation();
}

function loginForm(initialValues = {}) {
  return `<h2 class="form-title">Participant Sign In</h2><p class="subhead">Enter your assigned credentials to continue.</p>
    <form id="login-form">
      <div class="field"><label for="participant-id">Participant ID</label><input class="input" id="participant-id" name="participant_id" autocomplete="username" placeholder="part_…" minlength="5" maxlength="50" value="${esc(initialValues.participant_id || "")}" required></div>
      <div class="field"><label for="access-key">Access Key</label><input class="input" id="access-key" name="access_key" type="password" autocomplete="current-password" minlength="16" maxlength="128" value="${esc(initialValues.access_key || "")}" required></div>
      <button class="button button-block button-primary" type="submit">Sign In</button>
    </form><p id="credential-paste-status" class="copy-status" role="status" aria-live="polite"></p><div class="organizer-auth-notice"><span class="notice-eyebrow">ORGANIZER-CONTROLLED ACCESS</span><p class="form-foot">Participant credentials are created and managed exclusively by tournament organizers. Public self-registration is closed.</p></div>`;
}

function registerForm(initialValues = {}) {
  return `
    <h2 class="form-title">Register participant</h2>
    <p class="subhead">Create and submit participant credentials for the tournament.</p>
    <form id="register-form">
      <div class="field">
        <label for="reg-name">Participant Name <span>Required (2–100 chars)</span></label>
        <input class="input" id="reg-name" name="name" autocomplete="off" placeholder="e.g. DeepKnight AI" minlength="2" maxlength="100" value="${esc(initialValues.name || "")}" required>
      </div>
      <div class="field">
        <label for="reg-team">Team Name <span>Optional</span></label>
        <input class="input" id="reg-team" name="team_name" autocomplete="off" placeholder="e.g. Team Alpha" maxlength="100" value="${esc(initialValues.team_name || "")}">
      </div>
      <div class="field">
        <label for="reg-email">Contact Email <span>Optional</span></label>
        <input class="input" id="reg-email" name="email" type="email" autocomplete="email" placeholder="e.g. team@college.edu" maxlength="255" value="${esc(initialValues.email || "")}">
      </div>
      <button class="button button-block" type="submit">Register</button>
    </form>
    <p class="form-foot">Participant registration connects to the competition server. If organizer authorization is required by event policy, the server authorization requirement is enforced.</p>
  `;
}

function renderRegistrationSuccess(result) {
  stopLandingAnimation();
  stopArenaRefresh();
  stopOverviewPolling();
  document.body.classList.remove("public-interface");
  document.body.classList.add("entry-page");
  route = "credential-receipt";
  activeParticipant = null;

  app.innerHTML = `
    <section class="access-layout credential-layout" aria-label="Participant registration receipt">
      <div class="access-context">
        <div class="access-toolbar"><div class="section-index">AI KNOCKOUT <span>2026</span></div></div>
        <h1>Identity Created</h1>
        <p class="context-copy">Your participant registration is confirmed. Save your credentials now before proceeding.</p>
        <dl class="event-facts">
          <div><dt>Participant</dt><dd>${esc(result.name)}</dd></div>
          ${result.team_name ? `<div><dt>Team</dt><dd>${esc(result.team_name)}</dd></div>` : ""}
          <div><dt>Status</dt><dd>${esc(result.status)}</dd></div>
        </dl>
        <nav class="access-views" aria-label="Public navigation">
          <button class="access-nav-btn active" type="button" data-view="participant" aria-current="page">Participant</button>
          <button class="access-nav-btn" type="button" data-view="admin">Organizer</button>
          <button class="access-nav-btn" type="button" data-view="arena">Arena</button>
        </nav>
      </div>
      <figure class="access-board" aria-labelledby="access-board-caption">
        <div class="board-label"><span>STARTING POSITION</span><span class="board-coordinates">ILLUSTRATION</span></div>
        <div id="public-demo-board-container" class="cinematic-board-stage">
          ${renderCinematicBoard(INITIAL_POSITION_FEN)}
        </div>
        <figcaption id="access-board-caption">
          <span>Chessboard illustration</span>
        </figcaption>
      </figure>
      <section class="access-panel credential-panel" aria-label="Credentials receipt">
        <h2 class="form-title credential-title">Your Tournament Credentials</h2>
        <p class="subhead">Save these credentials immediately. The Access Key is shown only once and cannot be recovered.</p>

        <div class="credential">
          <label for="cred-participant-id">Participant ID</label>
          <div class="secret-row">
            <code id="cred-participant-id">${esc(result.participant_id)}</code>
            <button class="small-button" type="button" id="copy-participant-id" aria-label="Copy Participant ID">Copy ID</button>
          </div>
        </div>

        <div class="credential">
          <label for="cred-access-key">Access Key</label>
          <div class="secret-row">
            <code id="cred-access-key">${esc(result.access_key)}</code>
            <button class="small-button" type="button" id="copy-access-key" aria-label="Copy Access Key">Copy Key</button>
          </div>
        </div>

        <div class="credential-warning" role="alert">
          <strong>Important:</strong> Store your Access Key in a secure password manager. If lost, you will not be able to sign in or submit agents.
        </div>

        <p id="credential-copy-status" class="copy-status" role="status" aria-live="polite"></p>

        <div class="credential-actions">
          <button class="button button-secondary" type="button" id="copy-all-credentials">Copy Both</button>
          <button class="button button-primary" type="button" id="proceed-to-login">Proceed to Sign In</button>
        </div>
      </section>
    </section>`;

  app.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => renderPublicView(button.dataset.view)));

  const copyStatus = app.querySelector("#credential-copy-status");
  const writeClipboard = async (text, msg) => {
    try {
      if (typeof navigator !== "undefined" && navigator?.clipboard?.writeText) {
        await copyCredentialValue(text, (t) => navigator.clipboard.writeText(t));
      }
      if (copyStatus) copyStatus.textContent = msg;
      notify(msg);
    } catch {
      if (copyStatus) copyStatus.textContent = "Could not copy automatically. Please select and copy manually.";
    }
  };

  const copyIdBtn = app.querySelector("#copy-participant-id");
  if (copyIdBtn) {
    copyIdBtn.addEventListener("click", () => writeClipboard(result.participant_id, "Participant ID copied to clipboard."));
  }

  const copyKeyBtn = app.querySelector("#copy-access-key");
  if (copyKeyBtn) {
    copyKeyBtn.addEventListener("click", () => writeClipboard(result.access_key, "Access Key copied to clipboard."));
  }

  const copyAllBtn = app.querySelector("#copy-all-credentials");
  if (copyAllBtn) {
    copyAllBtn.addEventListener("click", () => {
      const combined = credentialsText(result.participant_id, result.access_key);
      writeClipboard(combined, "Both credentials copied to clipboard.");
    });
  }

  const proceedBtn = app.querySelector("#proceed-to-login");
  if (proceedBtn) {
    proceedBtn.addEventListener("click", () => {
      landing("login", "", { participant_id: result.participant_id, access_key: result.access_key });
    });
  }
}

function openingStudyMarkup() {
  return "";
}

function bindCredentialPaste() {
  const idInput = app.querySelector("#participant-id");
  const keyInput = app.querySelector("#access-key");
  if (!idInput || !keyInput) return;
  for (const [input, target] of [[idInput, "participant_id"], [keyInput, "access_key"]]) {
    input.addEventListener("paste", (event) => {
      const pasted = event.clipboardData?.getData("text");
      if (!pasted) return;
      const values = credentialPasteValues(pasted, idInput.value, keyInput.value, target);
      if (!values) {
        if (/\r?\n|participant\s+id\s*:|access\s+key\s*:/i.test(pasted)) {
          event.preventDefault();
          const pasteStatus = app.querySelector("#credential-paste-status");
          if (pasteStatus) pasteStatus.textContent = "Credentials not recognized. Nothing was pasted.";
        }
        return;
      }
      event.preventDefault();
      idInput.value = values.participant_id || "";
      keyInput.value = values.access_key || "";
      if (values.combined) {
        const pasteStatus = app.querySelector("#credential-paste-status");
        if (pasteStatus) pasteStatus.textContent = "Participant ID and Access Key filled.";
      }
    });
  }
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function withSubmitState(form, action, onError = null) {
  const button = form.querySelector("button[type=submit]");
  const old = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.dataset.busy = "true";
    button.textContent = "Connecting…";
  }
  try { await action(); }
  catch (error) {
    const msg = error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
    if (onError) {
      onError(msg);
    } else {
      landing(route, msg);
    }
  }
  finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      delete button.dataset.busy;
      button.textContent = old;
    }
  }
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await withSubmitState(form, async () => {
    const result = await api.login(formValues(form));
    sessionToken = result.access_token;
    dashboard(result.participant);
    await loadProfile();
  });
}

async function register(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = formValues(form);
  const name = (values.name || "").trim();
  const team_name = (values.team_name || "").trim();
  const email = (values.email || "").trim();

  if (name.length < 2) {
    landing("register", "Participant name must be at least 2 characters.", values);
    return;
  }
  if (name.length > 100) {
    landing("register", "Participant name exceeds 100 characters.", values);
    return;
  }
  if (/[\x00-\x1f<>]/.test(name)) {
    landing("register", "Participant name contains prohibited characters.", values);
    return;
  }
  if (team_name && /[\x00-\x1f<>]/.test(team_name)) {
    landing("register", "Team name contains prohibited characters.", values);
    return;
  }
  if (team_name.length > 100) {
    landing("register", "Team name exceeds 100 characters.", values);
    return;
  }
  if (email && !/^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/.test(email)) {
    landing("register", "Please enter a valid email address.", values);
    return;
  }

  const payload = {
    name,
    team_name: team_name || undefined,
    email: email || undefined,
  };

  const idempotencyKey = typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `reg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  await withSubmitState(form, async () => {
    const result = await api.register(payload, idempotencyKey);
    renderRegistrationSuccess(result);
  }, (errorMessage) => {
    landing("register", errorMessage, values);
  });
}

const portalSections = [
  ["welcome", "Welcome"],
  ["build", "Build"],
  ["submit", "Submit"],
  ["leaderboard", "Leaderboard"],
  ["live", "Live Match"],
];

function navIcon(name) {
  const paths = {
    welcome: '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/>',
    overview: '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/>',
    arena: '<path d="M14.5 17.5 3 6V3h3l11.5 11.5M13 19l6-6M19 13l2 2-4 4-2-2M15 5l4 4"/>',
    live: '<path d="M14.5 17.5 3 6V3h3l11.5 11.5M13 19l6-6M19 13l2 2-4 4-2-2M15 5l4 4"/>',
    build: '<path d="m8 7-4 5 4 5M16 7l4 5-4 5M14 5l-4 14"/>',
    submit: '<path d="M12 15V3m0 0L7.5 7.5M12 3l4.5 4.5M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>',
    matches: '<path d="M7 20h10M9 20v-3l-3-2 2-3-2-3 3-4 4 1 2 4-2 2 3 2 1 4h-5"/><circle cx="13" r=".7" cy="8"/>',
    results: '<path d="M7 4h10v4a5 5 0 0 1-10 0V4ZM7 6H4v2a4 4 0 0 0 4 4m9-6h3v2a4 4 0 0 1-4 4m-4 1v4m-4 3h8m-7-3h6"/>',
    leaderboard: '<path d="M7 4h10v4a5 5 0 0 1-10 0V4ZM7 6H4v2a4 4 0 0 0 4 4m9-6h3v2a4 4 0 0 1-4 4m-4 1v4m-4 3h8m-7-3h6"/>',
    profile: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2L20 12M4 12l2.7 5a7 7 0 0 0 11.7-2"/>',
  };
  return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.overview}</svg>`;
}

function dateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function badge(value = "Unavailable") {
  const key = String(value).toLowerCase();
  const tone = key.includes("eligible") || key.includes("approved") || key === "active" || key === "completed"
    ? "success" : key.includes("reject") || key.includes("fail") || key.includes("abort")
      ? "danger" : key.includes("validat") || key.includes("queued") || key.includes("starting") || key.includes("running")
        ? "warning" : "neutral";
  return `<span class="status-badge ${tone}">${esc(value.replaceAll("_", " "))}</span>`;
}

function shell(participant, active, title, content) {
  stopLandingAnimation();
  document.body.classList.remove("entry-page");
  document.body.classList.remove("public-interface");
  activeParticipant = participant;
  route = active;
  app.innerHTML = `<div class="portal-app">
    <header class="topbar"><a class="brand" href="#welcome" data-page="welcome" aria-label="AI Knockout participant home"><span class="brand-mark">AI</span><span class="brand-word">KNOCKOUT<small>CHESS ENGINEERING PLATFORM</small></span></a>
      <div class="topbar-actions">${themeToggleMarkup()}<button class="icon-button" type="button" id="refresh" aria-label="Refresh current page" title="Refresh">${navIcon("refresh")}</button><span class="participant-chip"><span class="avatar">${esc((participant.name || "P").slice(0, 1).toUpperCase())}</span><span>${esc(participant.participant_id)}</span></span><button class="text-button" type="button" data-signout>Sign out</button></div>
    </header>
    <div class="portal-frame"><main class="page-main"><div class="page-wrap"><div class="page-heading"><div><div class="eyebrow">AI KNOCKOUT <span>/</span> PARTICIPANT WORKSPACE</div><h1>${esc(title)}</h1></div></div><nav class="workspace-nav" aria-label="Participant sections">${portalSections.map(([key, label]) => { const selected = active === key || (key === "welcome" && active === "overview") || (key === "leaderboard" && ["bracket", "results", "leaderboard"].includes(active)) || (key === "live" && ["arena", "matches", "live"].includes(active)); return `<button type="button" class="workspace-link ${selected ? "active" : ""}" data-page="${key}" ${selected ? 'aria-current="page"' : ""}>${label}</button>`; }).join("")}</nav>${content}</div></main>
    </div></div>`;
  app.querySelectorAll("[data-signout]").forEach((button) => button.addEventListener("click", signOut));
  app.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(button.dataset.page, participant);
  }));
  app.querySelector("#refresh").addEventListener("click", () => navigate(active, participant));
}

let overviewPollTimer = null;

function stopOverviewPolling() {
  if (overviewPollTimer) {
    clearInterval(overviewPollTimer);
    overviewPollTimer = null;
  }
}

function overviewMarkup(participant) {
  return welcomeMarkup(participant);
}

function miniBoard() {
  return renderCinematicBoard("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
}

function welcomeMarkup(participant) {
  return `
    <div class="welcome-container">
      <section class="card welcome-hero-card">
        <div class="card-heading">
          <div>
            <span class="eyebrow">AI KNOCKOUT · AUTONOMOUS CHESS TOURNAMENT</span>
            <h2 style="font-size: 24px; margin-top: 4px;">Welcome, ${esc(participant.name || participant.participant_id)}</h2>
          </div>
          <span class="status-badge success">Registration Confirmed</span>
        </div>
        <p class="body-copy" style="font-size: 1.05rem; line-height: 1.6; margin-top: 10px; max-width: 820px;">
          Welcome to AI Knockout. In this competition, you engineer an autonomous chess intelligence module that competes in a fully arbitrated Swiss-system tournament. Build your engine following the submission contract, validate your archive against the server, and track your official tournament standing.
        </p>
      </section>

      <section class="welcome-action-grid">
        <article class="card welcome-action-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">STEP 1</span>
              <h2>Build Your Engine</h2>
            </div>
            <span class="card-mark blue">&lt;/&gt;</span>
          </div>
          <p class="body-copy" style="margin-bottom: 20px;">Review the standard <code>intelligence.py</code> contract, <code>choose_move(...)</code> signature, container I/O protocol, manifest schema, and archive packaging instructions.</p>
          <button class="button button-primary" type="button" data-page="build">Open Build Guide →</button>
        </article>

        <article class="card welcome-action-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">STEP 2</span>
              <h2>Submit Package</h2>
            </div>
            <span class="card-mark blue">↑</span>
          </div>
          <p class="body-copy" style="margin-bottom: 20px;">Upload your Python engine or <code>.tar</code> archive for automated static validation, checksum verification, and tournament registration within your quota.</p>
          <button class="button button-primary" type="button" data-page="submit">Go to Submit →</button>
        </article>
      </section>

      <section class="card welcome-status-card">
        <div class="card-heading">
          <div>
            <span class="eyebrow">EVENT OPERATIONS</span>
            <h2>Official Competition Status</h2>
          </div>
          <span id="welcome-event-pill" class="status-badge neutral">Checking…</span>
        </div>
        <div class="welcome-status-grid">
          <div class="welcome-status-item">
            <span class="muted-label">Participant ID</span>
            <strong style="font-size: 15px;">${esc(participant.participant_id)}</strong>
          </div>
          <div class="welcome-status-item">
            <span class="muted-label">Participant / Team</span>
            <strong style="font-size: 15px;">${esc(participant.name || "Solo Engineer")}</strong>
          </div>
          <div class="welcome-status-item">
            <span class="muted-label">Official Submission</span>
            <div id="welcome-official-status" style="font-size: 14px; font-weight: 600;">Checking official entry…</div>
          </div>
          <div class="welcome-status-item">
            <span class="muted-label">Tournament Phase</span>
            <strong id="welcome-tournament-phase" style="font-size: 15px;">Checking…</strong>
          </div>
        </div>
        <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--line); display: flex; gap: 12px; flex-wrap: wrap; align-items: center;">
          <button class="button button-secondary" type="button" data-page="leaderboard">View Official Leaderboard →</button>
          <button class="button button-secondary" type="button" data-page="live">Watch Live Match →</button>
        </div>
      </section>
    </div>
  `;
}

async function loadWelcome(participant, isBackground = false) {
  try {
    const [eventRes, subsRes] = await Promise.allSettled([
      api.eventStatus(sessionToken),
      api.submissions(sessionToken),
    ]);
    if (route !== "welcome" && route !== "overview") return;
    for (const result of [eventRes, subsRes]) {
      if (result.status === "rejected" && handleSessionError(result.reason)) return;
    }
    const event = eventRes.status === "fulfilled" ? eventRes.value : null;
    const subs = subsRes.status === "fulfilled" ? subsRes.value : null;

    const officialStatusEl = app.querySelector("#welcome-official-status");
    const phaseEl = app.querySelector("#welcome-tournament-phase");
    const eventPillEl = app.querySelector("#welcome-event-pill");

    // /participants/me/event-status is the server-authoritative source; it has no
    // phase/round fields, so only state what it actually reports.
    const frozen = Boolean(event?.official_frozen_at);
    if (phaseEl) {
      phaseEl.textContent = !event ? "Unavailable" : frozen ? "Official entries frozen" : "Build & submission window";
    }
    if (eventPillEl) {
      eventPillEl.className = `status-badge ${!event ? "neutral" : frozen ? "warning" : "success"}`;
      eventPillEl.textContent = !event ? "Status Unavailable" : frozen ? "Entries Frozen" : "Submissions Open";
    }

    if (officialStatusEl) {
      // The /submissions list only carries submission ids, so version state comes
      // from the event-status payload.
      const latest = event?.latest_submission || null;
      const official = event?.official_submission || null;
      const hasSubmission = Boolean(latest) || (subs?.items || []).length > 0;
      if (official) {
        officialStatusEl.innerHTML = `<span class="status-badge success">v${esc(official.version)} Official Entry</span>`;
      } else if (latest && latest.state === "ELIGIBLE") {
        officialStatusEl.innerHTML = `<span class="status-badge success">v${esc(latest.version)} Eligible</span>`;
      } else if (latest && latest.state === "APPROVED") {
        // Validation passed (practice matches allowed); official eligibility is a
        // separate organizer decision.
        officialStatusEl.innerHTML = `<span class="status-badge warning">v${esc(latest.version)} Validated</span>`;
      } else if (latest && latest.state === "REJECTED") {
        officialStatusEl.innerHTML = `<span class="status-badge danger">v${esc(latest.version)} Rejected</span>`;
      } else if (latest) {
        officialStatusEl.innerHTML = `<span class="status-badge warning">v${esc(latest.version)} Awaiting Review</span>`;
      } else if (hasSubmission) {
        officialStatusEl.innerHTML = `<span class="status-badge neutral">Submission Created</span>`;
      } else {
        officialStatusEl.innerHTML = `<span class="status-badge neutral">No Submission Yet</span>`;
      }
    }
  } catch (err) {
    if (!handleSessionError(err)) {
      const officialStatusEl = app.querySelector("#welcome-official-status");
      if (officialStatusEl) officialStatusEl.textContent = "Status unavailable";
    }
  }
}

function dashboard(participant) {
  stopOverviewPolling();
  shell(participant, "welcome", "Welcome to AI Knockout", welcomeMarkup(participant));
  loadWelcome(participant);
  overviewPollTimer = setInterval(() => {
    if (route === "welcome" || route === "overview") {
      loadWelcome(participant, true);
    } else {
      stopOverviewPolling();
    }
  }, 15000);
}

async function loadOverview(participant, isBackground = false) {
  return loadWelcome(participant, isBackground);
}

// Retained helper for practice match state formatting compatibility
function legacyOverviewMatchState(activeMatch) {
  if (!activeMatch) return null;
  const matchNote = activeMatch.state === "QUEUED"
    ? `Practice match queued vs ${activeMatch.house_ai?.name || "House AI"} · Awaiting worker dispatch (Sandbox unverified)`
    : `Match in progress vs ${activeMatch.house_ai?.name || "House AI"} · ${activeMatch.current_ply || 0} plies`;
  const buttonText = activeMatch.state === "QUEUED" ? "Match Queued…" : "Match In Progress…";
  const requestBtn = { disabled: false };
  requestBtn.disabled = !PARTICIPANT_CODE_EXECUTION_ENABLED;
  return { matchNote, buttonText, requestBtn };
}

function navigate(section, participant = activeParticipant) {
  if (!participant || !sessionToken) return landing("login");
  if (section !== "welcome" && section !== "overview") stopOverviewPolling();
  if (section !== "live") stopLiveMatchPolling();
  switch (section) {
    case "welcome":
    case "overview":
      dashboard(participant);
      break;
    case "build":
      renderBuild(participant);
      break;
    case "submit":
      renderSubmit(participant);
      break;
    case "leaderboard":
    case "results":
    case "bracket":
      renderLeaderboard(participant);
      break;
    case "live":
    case "arena":
    case "matches":
      renderLiveMatch(participant);
      break;
    case "profile":
      dashboard(participant);
      break;
    default:
      dashboard(participant);
  }
}

function renderPublicView(view) {
  stopLandingAnimation();
  if (view === "admin") {
    try { window.location.hash = "#admin"; } catch {}
    return renderAdmin("overview");
  }
  if (view === "arena") {
    try { window.location.hash = "#arena"; } catch {}
    return renderArena("live");
  }
  try { if (window.location.hash) window.location.hash = ""; } catch {}
  return landing("login");
}

function returnToParticipantAccess() {
  stopLandingAnimation();
  try { if (window.location.hash) window.location.hash = ""; } catch {}
  if (sessionToken) void signOut();
  else landing("login");
}

let liveMatchPollTimer = null;

function stopLiveMatchPolling() {
  if (liveMatchPollTimer) {
    clearTimeout(liveMatchPollTimer);
    liveMatchPollTimer = null;
  }
}

function renderLiveMatch(participant, matchId = null) {
  stopLiveMatchPolling();
  stopOverviewPolling();
  stopArenaRefresh();
  currentArenaMode = "tournament";

  shell(participant, "live", "Live Match Viewer", `
    <div class="live-match-container">
      <div id="live-match-notice"></div>
      <div id="live-match-stage">
        <div id="arena-context-bar" class="arena-context-bar" style="margin-bottom: 16px;">
          <div class="context-match-meta">
            <span id="arena-class-badge" class="context-classification-badge badge-official">OFFICIAL TOURNAMENT MATCH</span>
            <span id="arena-match-id-badge" class="context-match-id">Loading match…</span>
            <span id="arena-round-badge" class="context-round-name"></span>
          </div>
          <div class="context-match-controls" style="margin-left: auto; display: flex; gap: 8px; align-items: center;">
            <span id="arena-live-pulse-badge" class="arena-live-indicator live"><span class="live-dot"></span> LIVE SYNC</span>
            <button class="button button-secondary button-small" id="arena-btn-flip" type="button" title="Flip Board Perspective">Flip Board</button>
            <button class="button button-secondary button-small" id="live-match-refresh" type="button">Refresh ↻</button>
          </div>
        </div>

        <div class="arena-stage-grid">
          <div class="arena-board-column">
            <div class="arena-competitor-card top-competitor" id="arena-top-competitor">
              <div class="loading-copy" style="padding:10px;">Loading competitor…</div>
            </div>

            <div class="arena-board-viewport" id="arena-board-container" tabindex="0" role="region" aria-label="Official Chessboard">
              <div class="arena-waiting" role="status"><p>Loading authoritative position…</p></div>
            </div>

            <div class="arena-competitor-card bottom-competitor" id="arena-bottom-competitor">
              <div class="loading-copy" style="padding:10px;">Loading competitor…</div>
            </div>
          </div>

          <div class="arena-panel-column">
            <div class="arena-result-banner" id="arena-result-banner" style="display:none;"></div>

            <div class="arena-replay-toolbar">
              <div class="replay-buttons">
                <button class="button button-secondary button-small" id="replay-start" type="button" title="Start position (Home)">|◀</button>
                <button class="button button-secondary button-small" id="replay-prev" type="button" title="Previous ply (←)">◀</button>
                <span class="replay-step-label" id="replay-step-info">Start Position</span>
                <button class="button button-secondary button-small" id="replay-next" type="button" title="Next ply (→)">▶</button>
                <button class="button button-secondary button-small" id="replay-end" type="button" title="Latest ply (End)">▶|</button>
                <button class="button button-secondary button-small" id="replay-autoplay" type="button" title="Auto-play replay (Space)">Auto ▶</button>
              </div>
              <span class="replay-shortcut-hint">Keys: ← / → / Space</span>
            </div>

            <div class="arena-transcript-card">
              <div class="transcript-header">
                <h3>Authoritative Move Transcript</h3>
                <span class="ply-count-badge" id="arena-ply-count">0 plies</span>
              </div>
              <div class="transcript-scroll-area" id="arena-transcript-container">
                <div class="empty-transcript-copy" id="arena-no-moves-note" style="padding: 16px; color: var(--muted); font-size: 13px;">
                  No moves played yet.
                </div>
              </div>
            </div>

            <details class="arena-diagnostics-card" id="arena-diagnostics-details">
              <summary>Technical Execution Diagnostics & Policy Verification</summary>
              <div class="diagnostics-content" id="arena-diagnostics-body">
                <div><span class="muted-label">Rules Engine:</span> <code id="diag-rules-engine">Standard FIDE Chess (python-chess)</code></div>
                <div><span class="muted-label">Runtime Profile:</span> <code id="diag-runtime-profile">cgroup_v2_strict_sandbox</code></div>
                <div><span class="muted-label">Clock Setting:</span> <span id="diag-clock-setting">180s initial + 2s inc</span></div>
                <div><span class="muted-label">Worker Isolation:</span> <span id="diag-worker-isolation">Isolated container per side</span></div>
              </div>
            </details>
          </div>
        </div>
      </div>
    </div>
  `);

  wireLiveMatchControls(participant);
  loadLiveMatchData(participant, matchId);
}

function wireLiveMatchControls(participant) {
  app.querySelector("#arena-btn-flip")?.addEventListener("click", () => {
    arenaBoardFlipped = !arenaBoardFlipped;
    if (currentArenaMatchData) {
      updateGrandmasterArena(currentArenaMatchData, "tournament", currentArenaMatchData._historyList || []);
    }
  });

  app.querySelector("#live-match-refresh")?.addEventListener("click", () => {
    loadLiveMatchData(participant, currentArenaMatchId);
  });

  app.querySelector("#replay-start")?.addEventListener("click", () => arenaJumpReplay(0));
  app.querySelector("#replay-prev")?.addEventListener("click", () => arenaStepReplay(-1));
  app.querySelector("#replay-next")?.addEventListener("click", () => arenaStepReplay(1));
  app.querySelector("#replay-end")?.addEventListener("click", () => {
    if (currentArenaMatchData?.moves) arenaJumpReplay(currentArenaMatchData.moves.length);
  });
  app.querySelector("#replay-autoplay")?.addEventListener("click", toggleArenaAutoPlay);

  window.addEventListener("keydown", (e) => {
    if (route !== "live") return;
    if (e.target?.tagName === "INPUT" || e.target?.tagName === "TEXTAREA") return;
    if (e.key === "ArrowLeft") { e.preventDefault(); arenaStepReplay(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); arenaStepReplay(1); }
    else if (e.key === "Home") { e.preventDefault(); arenaJumpReplay(0); }
    else if (e.key === "End") {
      e.preventDefault();
      if (currentArenaMatchData?.moves) arenaJumpReplay(currentArenaMatchData.moves.length);
    } else if (e.key === " " && e.target === document.body) {
      e.preventDefault();
      toggleArenaAutoPlay();
    }
  });
}

async function loadLiveMatchData(participant, matchId = null) {
  const noticeNode = app.querySelector("#live-match-notice");
  const stageNode = app.querySelector("#live-match-stage");
  if (!noticeNode || !stageNode) return;

  try {
    const matches = await api.tournamentMatches();
    const items = Array.isArray(matches) ? matches : (Array.isArray(matches?.items) ? matches.items : []);
    const officialItems = items.filter((m) => m.purpose === "OFFICIAL" || (!m.house_ai && (m.white_name || m.black_name)));

    if (!officialItems.length) {
      noticeNode.innerHTML = `
        <div class="notice-callout info">
          <i>ℹ</i>
          <div>
            <strong>No official tournament matches scheduled yet</strong>
            <p>Official match pairings will appear here automatically once Swiss rounds commence.</p>
          </div>
        </div>
      `;
      stageNode.style.display = "none";
      return;
    }

    stageNode.style.display = "";
    let target = null;
    if (matchId) {
      target = officialItems.find((m) => String(m.match_id) === String(matchId)) || officialItems[0];
    } else {
      target = officialItems.find((m) => ["RUNNING", "STARTING"].includes(m.state))
        || officialItems.find((m) => m.state === "QUEUED")
        || officialItems.find((m) => ["COMPLETED", "ABORTED"].includes(m.state))
        || officialItems[0];
    }

    if (["RUNNING", "STARTING"].includes(target.state)) {
      noticeNode.innerHTML = "";
    } else if (target.state === "QUEUED") {
      noticeNode.innerHTML = `
        <div class="notice-callout info" style="margin-bottom: 12px;">
          <i>ℹ</i>
          <div>
            <strong>No active match currently in progress</strong>
            <p>Showing scheduled official match awaiting start. Plies will stream live once the game commences.</p>
          </div>
        </div>
      `;
    } else {
      noticeNode.innerHTML = `
        <div class="notice-callout info" style="margin-bottom: 12px;">
          <i>ℹ</i>
          <div>
            <strong>No active match currently in progress</strong>
            <p>Showing concluded official tournament match record.</p>
          </div>
        </div>
      `;
    }

    const fullMatch = await api.tournamentMatch(target.match_id);
    fullMatch._historyList = officialItems;
    updateGrandmasterArena(fullMatch, "tournament", officialItems);

    if (["RUNNING", "STARTING"].includes(fullMatch.state)) {
      stopLiveMatchPolling();
      liveMatchPollTimer = setTimeout(() => {
        if (route === "live") loadLiveMatchData(participant, target.match_id);
      }, 3000);
    }
  } catch (err) {
    if (!handleSessionError(err)) {
      noticeNode.innerHTML = `<div class="notice-callout warning"><i>!</i><div><strong>Match service unavailable</strong><p>${esc(err.message || "Failed to load tournament match.")}</p></div></div>`;
    }
  }
}

let currentArenaMode = "spectator";
let currentArenaMatchId = null;
let currentArenaMatchData = null;
let currentArenaPlyIdx = -1;
let arenaBoardFlipped = false;

const arenaNavigation = [
  ["practice", "Practice Arena"],
  ["tournament", "Tournament Arena"],
  ["spectator", "Spectator Arena"],
  ["bracket", "Tournament Rounds"],
  ["leaderboard", "Leaderboard"],
];

function renderArena(active = "live", matchId = null) {
  stopLandingAnimation();
  stopArenaRefresh();
  route = `arena-${active}`;

  if (active === "bracket") {
    document.body.classList.remove("entry-page");
    document.body.classList.add("public-interface");
    app.innerHTML = `
      <div class="grandmaster-arena-shell spectator-app">
        <header class="arena-topbar spectator-header">
          <div class="arena-topbar-left">
            <a class="arena-brand-badge spectator-brand" href="#arena" data-view="arena"><span class="arena-brand-logo">AI</span><span class="arena-brand-name">KNOCKOUT <small>CHESS ARENA</small></span></a>
            <nav class="arena-mode-nav" aria-label="Arena modes">
              <button type="button" class="arena-mode-btn" data-arena-mode="practice">Practice</button>
              <button type="button" class="arena-mode-btn" data-arena-mode="tournament">Tournament</button>
              <button type="button" class="arena-mode-btn" data-arena-mode="spectator">Spectator</button>
              <button type="button" class="arena-mode-btn active" data-arena-page="bracket">Rounds</button>
              <button type="button" class="arena-mode-btn" data-arena-page="leaderboard">Standings</button>
            </nav>
          </div>
          <div class="arena-topbar-right">
            <button class="public-refresh button button-secondary button-small" id="arena-refresh" type="button">Refresh ↻</button>
            ${activeParticipant && sessionToken ? '<button class="button button-secondary button-small" type="button" data-page="overview">← Workspace</button>' : '<button class="button button-primary button-small" type="button" data-view="participant">Participant Access</button>'}
          </div>
        </header>
        <main class="spectator-main" id="arena-main"><div class="loading-copy" style="padding: 40px; text-align: center;">Loading tournament data…</div></main>
      </div>
    `;
    wireArenaShellHeader("bracket");
    loadArenaBracket();
    return;
  }

  if (active === "leaderboard") {
    document.body.classList.remove("entry-page");
    document.body.classList.add("public-interface");
    app.innerHTML = `
      <div class="grandmaster-arena-shell spectator-app">
        <header class="arena-topbar spectator-header">
          <div class="arena-topbar-left">
            <a class="arena-brand-badge spectator-brand" href="#arena" data-view="arena"><span class="arena-brand-logo">AI</span><span class="arena-brand-name">KNOCKOUT <small>CHESS ARENA</small></span></a>
            <nav class="arena-mode-nav" aria-label="Arena modes">
              <button type="button" class="arena-mode-btn" data-arena-mode="practice">Practice</button>
              <button type="button" class="arena-mode-btn" data-arena-mode="tournament">Tournament</button>
              <button type="button" class="arena-mode-btn" data-arena-mode="spectator">Spectator</button>
              <button type="button" class="arena-mode-btn" data-arena-page="bracket">Rounds</button>
              <button type="button" class="arena-mode-btn active" data-arena-page="leaderboard">Standings</button>
            </nav>
          </div>
          <div class="arena-topbar-right">
            <button class="public-refresh button button-secondary button-small" id="arena-refresh" type="button">Refresh ↻</button>
            ${activeParticipant && sessionToken ? '<button class="button button-secondary button-small" type="button" data-page="overview">← Workspace</button>' : '<button class="button button-primary button-small" type="button" data-view="participant">Participant Access</button>'}
          </div>
        </header>
        <main class="spectator-main" id="arena-main"><div class="loading-copy" style="padding: 40px; text-align: center;">Loading tournament data…</div></main>
      </div>
    `;
    wireArenaShellHeader("leaderboard");
    loadArenaLeaderboard();
    return;
  }

  // Core match modes: practice, tournament, spectator (or live)
  if (active === "practice") currentArenaMode = "practice";
  else if (active === "tournament") currentArenaMode = "tournament";
  else currentArenaMode = "spectator";

  if (matchId) currentArenaMatchId = matchId;

  document.body.classList.remove("entry-page");
  if (activeParticipant && sessionToken) {
    document.body.classList.remove("public-interface");
  } else {
    document.body.classList.add("public-interface");
  }

  app.innerHTML = `
    <div class="grandmaster-arena-shell spectator-app">
      <header class="arena-topbar spectator-header">
        <div class="arena-topbar-left">
          <a class="arena-brand-badge spectator-brand" href="#arena" data-view="arena">
            <span class="arena-brand-logo">AI</span>
            <span class="arena-brand-name">KNOCKOUT <small>CHESS ARENA</small></span>
          </a>
          <nav class="arena-mode-nav" aria-label="Arena modes">
            <button type="button" class="arena-mode-btn ${currentArenaMode === "practice" ? "active" : ""}" data-arena-mode="practice">Practice</button>
            <button type="button" class="arena-mode-btn ${currentArenaMode === "tournament" ? "active" : ""}" data-arena-mode="tournament">Tournament</button>
            <button type="button" class="arena-mode-btn ${currentArenaMode === "spectator" ? "active" : ""}" data-arena-mode="spectator">Spectator</button>
            <button type="button" class="arena-mode-btn" data-arena-page="bracket">Rounds</button>
            <button type="button" class="arena-mode-btn" data-arena-page="leaderboard">Standings</button>
          </nav>
        </div>

        <div class="arena-topbar-right">
          <span id="arena-live-pulse-badge" class="arena-live-indicator live">
            <span class="live-dot"></span> LIVE SYNC
          </span>
          <button class="button button-secondary button-small" id="arena-btn-flip" type="button" title="Flip Board Perspective">
            Flip Board
          </button>
          <button class="public-refresh button button-secondary button-small" id="arena-refresh" type="button" title="Refresh authoritative state">
            ↻ Refresh
          </button>
          ${activeParticipant && sessionToken ? `
            <button class="button button-secondary button-small" type="button" data-page="overview">
              ← Workspace
            </button>
          ` : `
            <button class="button button-primary button-small" type="button" data-view="participant">
              Participant Access
            </button>
          `}
        </div>
      </header>

      <main class="spectator-main" id="arena-main">
        <div id="arena-context-bar" class="arena-context-bar">
          <div class="context-match-meta">
            ${currentArenaMode === "practice"
              ? '<span id="arena-class-badge" class="context-classification-badge badge-practice">PRACTICE MATCH (HOUSE AI / MOCK)</span>'
              : '<span id="arena-class-badge" class="context-classification-badge badge-official">OFFICIAL TOURNAMENT MATCH</span>'}
            <span id="arena-match-id-badge" class="context-match-id">Loading match…</span>
            <span id="arena-round-badge" class="context-round-name"></span>
          </div>
          <div id="arena-quick-controls" class="context-match-controls"></div>
        </div>

        <div class="arena-main-stage" id="arena-main-stage">
          <div class="arena-stage-grid">
            <div class="arena-board-column">
              <div class="arena-competitor-card top-competitor" id="arena-top-competitor">
                <div class="loading-copy" style="padding:10px;">Loading competitor…</div>
              </div>

              <div class="arena-board-viewport arena-board-placeholder" id="arena-board-container" tabindex="0" role="region" aria-label="Grandmaster Chessboard">
                <div class="arena-waiting" role="status"><p>Loading authoritative position…</p></div>
              </div>

              <div class="arena-competitor-card bottom-competitor" id="arena-bottom-competitor">
                <div class="loading-copy" style="padding:10px;">Loading competitor…</div>
              </div>
            </div>

            <div class="arena-panel-column">
              <div class="arena-result-banner" id="arena-result-banner" style="display:none;"></div>

              <div class="arena-replay-toolbar">
                <div class="replay-buttons">
                  <button class="button button-secondary button-small" id="replay-start" type="button" title="Start position (Home)">|◀</button>
                  <button class="button button-secondary button-small" id="replay-prev" type="button" title="Previous ply (←)">◀</button>
                  <span class="replay-step-label" id="replay-step-info">Start Position</span>
                  <button class="button button-secondary button-small" id="replay-next" type="button" title="Next ply (→)">▶</button>
                  <button class="button button-secondary button-small" id="replay-end" type="button" title="Final position (End)">▶|</button>
                  <button class="button button-secondary button-small" id="replay-autoplay" type="button" title="Auto-play moves (Space)">▶ Auto</button>
                </div>
                <span class="replay-shortcut-hint">Keys: ← → Home End Space</span>
              </div>

              <div class="arena-transcript-card">
                <div class="transcript-header">
                  <h3>Move History & Transcript</h3>
                  <span class="ply-count-badge" id="arena-ply-count">0 plies</span>
                </div>
                <div class="transcript-scroll-area">
                  <table class="data-table move-transcript-table" id="arena-move-table">
                    <thead>
                      <tr>
                        <th>Ply</th>
                        <th>Turn</th>
                        <th>Move</th>
                        <th>Latency</th>
                      </tr>
                    </thead>
                    <tbody id="arena-move-tbody"></tbody>
                  </table>
                  <p class="match-history-note" id="arena-no-moves-note" style="display:none;">Move history is not available from the current API.</p>
                </div>
              </div>

              <details class="arena-diagnostics-card" id="arena-diagnostics-details">
                <summary>Technical Execution Diagnostics & Policy Verification</summary>
                <div class="diagnostics-content" id="arena-diagnostics-content">
                  <span class="muted-copy">Policy verification details available for active matches.</span>
                </div>
              </details>
            </div>
          </div>

          <section class="arena-lower-section arena-lower" id="arena-lower-section"></section>
        </div>
      </main>
    </div>
  `;

  wireArenaShellHeader(currentArenaMode);
  loadGrandmasterArenaData(currentArenaMode, currentArenaMatchId);
}

function wireArenaShellHeader(currentMode) {
  app.querySelectorAll("[data-arena-mode]").forEach((btn) => {
    btn.addEventListener("click", () => renderArena(btn.dataset.arenaMode));
  });
  app.querySelectorAll("[data-arena-page]").forEach((btn) => {
    btn.addEventListener("click", () => renderArena(btn.dataset.arenaPage));
  });
  app.querySelectorAll('[data-view="arena"]').forEach((btn) => {
    btn.addEventListener("click", (e) => { e.preventDefault(); renderArena("spectator"); });
  });
  app.querySelector('[data-view="participant"]')?.addEventListener("click", returnToParticipantAccess);
  app.querySelector('[data-page="overview"]')?.addEventListener("click", () => {
    if (activeParticipant) dashboard(activeParticipant);
  });
  app.querySelector("#arena-btn-flip")?.addEventListener("click", () => {
    arenaBoardFlipped = !arenaBoardFlipped;
    if (currentArenaMatchData) {
      updateGrandmasterArena(currentArenaMatchData, currentArenaMode, currentArenaMatchData._historyList || []);
    }
  });
  app.querySelector("#arena-refresh")?.addEventListener("click", () => {
    loadGrandmasterArenaData(currentArenaMode, currentArenaMatchId);
  });
}

async function loadGrandmasterArenaData(mode, matchId) {
  if (arenaLiveRequestPending) return;
  arenaLiveRequestPending = true;
  const stage = app.querySelector("#arena-main-stage");
  if (!stage) { arenaLiveRequestPending = false; return; }

  try {
    if (mode === "practice") {
      if (!sessionToken || !activeParticipant) {
        const matchIdBadge = app.querySelector("#arena-match-id-badge");
        if (matchIdBadge) matchIdBadge.textContent = "Sign-in required";
        stage.innerHTML = `
          <div class="notice-callout info" style="margin: 40px auto; max-width: 600px; text-align: center;">
            <h3>Participant Authentication Required</h3>
            <p>Participant-code execution is disabled pending sandbox security verification. Signing in will not start a match; the arena will become available after the release gates are verified.</p>
            <div style="margin-top: 16px;">
              <button class="button button-primary" type="button" data-view="participant">Sign in as Participant →</button>
            </div>
          </div>
        `;
        stage.querySelector('[data-view="participant"]')?.addEventListener("click", returnToParticipantAccess);
        const liveBadge = app.querySelector("#arena-live-pulse-badge");
        if (liveBadge) liveBadge.className = "arena-live-indicator idle";
        return;
      }

      const res = await api.developmentMatches(sessionToken);
      const matches = res.items || [];
      let target = null;
      if (matchId) {
        target = matches.find((m) => m.match_id === matchId) || { match_id: matchId };
      } else {
        target = matches.find((m) => ["RUNNING", "STARTING", "QUEUED"].includes(m.state)) || matches[0];
      }

      if (target) {
        const fullMatch = await api.developmentMatch(sessionToken, target.match_id);
        fullMatch._historyList = matches;
        updateGrandmasterArena(fullMatch, "practice", matches);
      } else {
        renderPracticeEmptyLauncher(stage);
      }
    } else {
      // mode === "tournament" or "spectator"
      const matches = await api.tournamentMatches();
      const items = Array.isArray(matches) ? matches : (Array.isArray(matches?.items) ? matches.items : []);
      let target = null;
      if (matchId) {
        target = items.find((m) => String(m.match_id) === String(matchId)) || items[0];
      } else {
        target = items.find((m) => m.state === "RUNNING") || items.find((m) => m.state === "COMPLETED") || items[0];
      }

      if (target) {
        const fullMatch = await api.tournamentMatch(target.match_id);
        fullMatch._historyList = items;
        updateGrandmasterArena(fullMatch, mode, items);
      } else {
        const matchIdBadge = app.querySelector("#arena-match-id-badge");
        if (matchIdBadge) matchIdBadge.textContent = "No official matches";
        stage.innerHTML = `
          <div class="arena-unavailable" style="text-align: center; padding: 40px;">
            <span class="eyebrow">OFFICIAL TOURNAMENT ARENA</span>
            <h2>No Official Matches Recorded Yet</h2>
            <p>Official tournament matches will appear here automatically once Swiss rounds commence.</p>
          </div>
        `;
      }
    }
  } catch (err) {
    const matchIdBadge = app.querySelector("#arena-match-id-badge");
    if (matchIdBadge) matchIdBadge.textContent = "Arena temporarily unavailable";
    if (stage) {
      stage.innerHTML = `
        <div class="notice-callout warning arena-load-error" role="alert">
          <div>
            <strong>We couldn’t load arena data.</strong>
            <p>The live match service did not return usable match records. Try again shortly.</p>
          </div>
          <button class="button button-secondary" id="arena-retry-load" type="button">Retry</button>
        </div>
      `;
      stage.querySelector("#arena-retry-load")?.addEventListener("click", () => {
        if (arenaRefreshTimer) clearTimeout(arenaRefreshTimer);
        arenaRefreshTimer = null;
        loadGrandmasterArenaData(mode, matchId);
      });
    }
    const liveBadge = app.querySelector("#arena-live-pulse-badge");
    if (liveBadge) {
      liveBadge.className = "arena-live-indicator idle";
      liveBadge.innerHTML = '<span class="live-dot"></span> RECONNECTING…';
    }
    arenaRefreshTimer = setTimeout(() => {
      loadGrandmasterArenaData(mode, matchId);
    }, 5000);
  } finally {
    arenaLiveRequestPending = false;
  }
}

function renderPracticeEmptyLauncher(stage) {
  const matchIdBadge = app.querySelector("#arena-match-id-badge");
  if (matchIdBadge) matchIdBadge.textContent = "No practice matches";
  stage.innerHTML = `
    <div style="max-width: 680px; margin: 32px auto; padding: 24px; background: var(--surface-raised); border: 1px solid var(--line); border-radius: 8px;">
      <div style="margin-bottom: 12px;">
        <span class="phase-pill"><i></i> PRACTICE MATCH</span>
        <h2 style="margin: 4px 0 0 0; font-size: 20px;">Play Your First Practice Match vs House AI</h2>
      </div>
      <p class="body-copy" style="margin-bottom: 16px;">
        ${PARTICIPANT_CODE_EXECUTION_ENABLED ? "Run a test match between your validated submission and the House AI engine." : "Participant-code execution is disabled pending sandbox security verification. No submission will be run."}
        <em>Test matches do NOT affect official Swiss tournament standings or qualification rankings.</em>
      </p>
      ${PARTICIPANT_CODE_EXECUTION_ENABLED ? `
        <button class="button button-primary" id="arena-empty-launch-btn" type="button" title="Set up a practice match">Setup Practice Match →</button>
      ` : `
        <div class="practice-status-wrap">
          <span class="status-badge neutral">Sandbox Standby</span>
          <span class="muted-copy" style="font-size: 13px;">Participant-code execution is in standby mode pending sandbox verification. Official matches are arbitrated by event organizers.</span>
          <button class="button button-primary" id="arena-empty-launch-btn" type="button" disabled style="display:none;" title="Participant-code execution is disabled pending sandbox verification.">Execution disabled</button>
        </div>
      `}
    </div>
  `;
  stage.querySelector("#arena-empty-launch-btn")?.addEventListener("click", () => {
    if (activeParticipant) openTestMatchModal(activeParticipant);
  });
}

function updateGrandmasterArena(match, mode, matchHistoryList = []) {
  currentArenaMatchData = match;
  currentArenaMatchId = match.match_id;

  const state = match.state;
  const isRunning = ["QUEUED", "STARTING", "RUNNING"].includes(state);
  const isCompleted = ["COMPLETED", "ABORTED"].includes(state);
  const isOfficial = match.purpose === "OFFICIAL" || (!match.house_ai && (match.white_name || match.black_name));
  const isTestMax = !isOfficial && match.house_ai?.house_ai_id === "house-ai-maximum-difficulty";
  const isTestUnified = !isOfficial && match.house_ai?.house_ai_id === "AIK-HOUSE-001";

  // Context bar
  const classBadge = app.querySelector("#arena-class-badge");
  if (classBadge) {
    if (isOfficial) {
      classBadge.className = "context-classification-badge badge-official";
      classBadge.textContent = "OFFICIAL TOURNAMENT MATCH";
    } else if (isTestUnified) {
      classBadge.className = "context-classification-badge badge-practice";
      classBadge.textContent = "TEST MATCH · UNIFIED HOUSE AI (AIK-HOUSE-001)";
    } else if (isTestMax) {
      classBadge.className = "context-classification-badge badge-practice";
      classBadge.textContent = "TEST MATCH · HOUSE AI (MAXIMUM DIFFICULTY)";
    } else {
      classBadge.className = "context-classification-badge badge-practice";
      classBadge.textContent = "PRACTICE MATCH (HOUSE AI / MOCK)";
    }
  }

  const matchIdBadge = app.querySelector("#arena-match-id-badge");
  if (matchIdBadge) {
    let stateLabel = state;
    if (state === "QUEUED") stateLabel = "QUEUED (Awaiting Start)";
    else if (state === "STARTING") stateLabel = "STARTING (Engines Initializing)";
    else if (state === "RUNNING") stateLabel = `RUNNING (Ply ${match.current_ply || 0})`;
    else if (state === "COMPLETED") stateLabel = `COMPLETED (${match.result?.outcome || match.result_outcome || "Concluded"})`;
    else if (state === "ABORTED") stateLabel = `ABORTED (${match.result?.failure || match.failure_reason || "Halted"})`;
    else if (state === "FAILED") stateLabel = "FAILED (Execution Error)";
    matchIdBadge.textContent = `Match #${String(match.match_id).slice(0, 8)} · ${stateLabel}`;
  }

  const roundBadge = app.querySelector("#arena-round-badge");
  if (roundBadge) {
    roundBadge.textContent = match.round_name ? `(${match.round_name})` : isOfficial ? "Official Swiss Pairing" : "Practice Match";
  }

  const quickControls = app.querySelector("#arena-quick-controls");
  if (quickControls) {
    if (mode === "practice") {
      quickControls.innerHTML = `<button class="button button-primary button-small" id="arena-btn-new-practice" type="button">New Practice Match</button>`;
      quickControls.querySelector("#arena-btn-new-practice")?.addEventListener("click", () => {
        if (activeParticipant) openTestMatchModal(activeParticipant);
      });
    } else if (matchHistoryList.length > 0) {
      quickControls.innerHTML = `
        <select class="select-input" id="arena-select-match" style="max-width: 280px; font-size: 11px; padding: 4px 8px;">
          ${matchHistoryList.map((m) => `
            <option value="${m.match_id}" ${m.match_id === match.match_id ? "selected" : ""}>
              Match #${String(m.match_id).slice(0, 6)} · ${esc(m.white_name || "White")} vs ${esc(m.black_name || "Black")} (${m.state})
            </option>
          `).join("")}
        </select>
      `;
      quickControls.querySelector("#arena-select-match")?.addEventListener("change", (e) => {
        loadGrandmasterArenaData(mode, e.target.value);
      });
    }
  }

  // Competitor resolution
  let whiteComp = { name: "White", meta: "Tournament Competitor", isHouse: false };
  let blackComp = { name: "Black", meta: "Tournament Competitor", isHouse: false };

  if (isOfficial) {
    whiteComp.name = match.white_name || "White";
    blackComp.name = match.black_name || "Black";
  } else {
    const partSide = match.participant?.color || "WHITE";
    const partName = activeParticipant?.name || (match.participant?.submission_id ? `Participant AI · v${match.participant.submission_version}` : "Participant AI");
    const partMeta = `v${match.participant?.submission_version || 1} · SHA-256: ${esc((match.participant?.artifact_sha256 || "").slice(0, 10))}…`;

    const houseName = isTestMax
      ? "House AI (Maximum Difficulty)"
      : (match.house_ai?.house_ai_id ? `${match.house_ai.house_ai_id} · v${match.house_ai.version ?? "1"}` : "House AI");
    const houseMeta = isTestMax ? "Iterative Minimax (Depth 4)" : "Baseline Opponent";

    if (partSide === "WHITE") {
      whiteComp = { name: partName, meta: partMeta, isHouse: false };
      blackComp = { name: houseName, meta: houseMeta, isHouse: true };
    } else {
      whiteComp = { name: houseName, meta: houseMeta, isHouse: true };
      blackComp = { name: partName, meta: partMeta, isHouse: false };
    }
  }

  // Turn calculation
  const sideToMove = match.side_to_move || (match.current_fen ? match.current_fen.split(" ")[1] : "w");
  const isWhiteTurn = isRunning && (sideToMove === "w" || String(sideToMove).toUpperCase() === "WHITE");
  const isBlackTurn = isRunning && (sideToMove === "b" || String(sideToMove).toUpperCase() === "BLACK");

  // Material calculation
  const balance = calculateMaterialBalance(match.current_fen);

  const renderCard = (comp, color, isTurn, clockMs, captured, diff) => `
    <div class="competitor-left">
      <div class="side-avatar ${color}">${color === "white" ? "♔" : "♚"}</div>
      <div class="competitor-info">
        <span class="competitor-name">${esc(comp.name)}</span>
        <div class="competitor-meta">
          <span>${esc(comp.meta)}</span>
          ${captured.length ? `<span class="captured-pieces-bar">${captured.join(" ")}${diff > 0 ? `<strong class="captured-score-diff">+${diff}</strong>` : ""}</span>` : ""}
        </div>
      </div>
    </div>
    <div class="competitor-right">
      <span class="turn-indicator ${isTurn ? "active" : ""}">${isTurn ? "● TO MOVE" : "WAITING"}</span>
      <span class="digital-clock ${isTurn ? "clock-active" : ""}">${formatClock(clockMs)}</span>
    </div>
  `;

  const topCard = app.querySelector("#arena-top-competitor");
  const bottomCard = app.querySelector("#arena-bottom-competitor");

  if (arenaBoardFlipped) {
    if (topCard) topCard.innerHTML = renderCard(whiteComp, "white", isWhiteTurn, match.white_remaining_ms, balance.whiteCaptured, balance.diff > 0 ? balance.diff : 0);
    if (bottomCard) bottomCard.innerHTML = renderCard(blackComp, "black", isBlackTurn, match.black_remaining_ms, balance.blackCaptured, balance.diff < 0 ? Math.abs(balance.diff) : 0);
  } else {
    if (topCard) topCard.innerHTML = renderCard(blackComp, "black", isBlackTurn, match.black_remaining_ms, balance.blackCaptured, balance.diff < 0 ? Math.abs(balance.diff) : 0);
    if (bottomCard) bottomCard.innerHTML = renderCard(whiteComp, "white", isWhiteTurn, match.white_remaining_ms, balance.whiteCaptured, balance.diff > 0 ? balance.diff : 0);
  }

  // Update board
  const boardContainer = app.querySelector("#arena-board-container");
  const latestMove = Array.isArray(match.moves) && match.moves.length > 0 ? match.moves[match.moves.length - 1] : null;
  if (boardContainer) {
    updateCinematicBoard(boardContainer, match.current_fen || INITIAL_POSITION_FEN, {
      from: latestMove?.from_square,
      to: latestMove?.to_square,
      flipped: arenaBoardFlipped,
    });
  }

  // Result Banner
  const resultBanner = app.querySelector("#arena-result-banner");
  if (resultBanner) {
    if (isCompleted) {
      resultBanner.style.display = "block";
      const outcome = match.result?.outcome || match.result_outcome || state;
      const reason = match.result?.reason || match.result_reason || "Match concluded";
      const failure = match.result?.failure;
      let winnerText = "Match Completed";
      if (outcome.includes("WHITE_WON")) winnerText = `${esc(whiteComp.name)} Won (1-0)`;
      else if (outcome.includes("BLACK_WON")) winnerText = `${esc(blackComp.name)} Won (0-1)`;
      else if (outcome.includes("DRAW")) winnerText = `½-½ Drawn Game`;

      resultBanner.innerHTML = `
        <div class="result-banner-header">
          <strong class="result-title">${winnerText}</strong>
          <span class="status-badge success">${esc(outcome)}</span>
        </div>
        <p class="result-reason">Termination Reason: <strong>${esc(reason)}</strong>${failure ? ` · <span style="color:var(--danger);">${esc(failure)}</span>` : ""} · <strong>${resultSourceLabel(match.result_source)}</strong></p>
        <div class="result-actions">
          <button class="button button-secondary button-small" id="btn-replay-from-start" type="button">|◀ Replay from Start</button>
        </div>
      `;
      resultBanner.querySelector("#btn-replay-from-start")?.addEventListener("click", () => arenaJumpReplay(-1));
    } else {
      resultBanner.style.display = "none";
    }
  }

  // Replay & Move Transcript
  const hasMoves = Array.isArray(match.moves) && match.moves.length > 0;
  const tbody = app.querySelector("#arena-move-tbody");
  const noMovesNote = app.querySelector("#arena-no-moves-note");
  const plyCountBadge = app.querySelector("#arena-ply-count");

  if (plyCountBadge) plyCountBadge.textContent = `${match.moves?.length || 0} plies`;

  if (hasMoves && tbody) {
    currentArenaPlyIdx = match.moves.length - 1;
    tbody.innerHTML = match.moves.map((m, idx) => `
      <tr class="move-transcript-row ${idx === currentArenaPlyIdx ? "active-ply" : ""}" data-ply-idx="${idx}">
        <td>#${m.ply}</td>
        <td>${m.ply % 2 === 1 ? "White" : "Black"}</td>
        <td><strong><code>${esc(m.move_uci)}</code></strong></td>
        <td><small>${m.elapsed_ms != null ? `${m.elapsed_ms} ms` : "—"}</small></td>
      </tr>
    `).join("");

    if (noMovesNote) noMovesNote.style.display = "none";

    const stepLabel = app.querySelector("#replay-step-info");
    if (stepLabel) stepLabel.textContent = `Ply ${match.moves.length} of ${match.moves.length}`;

    tbody.querySelectorAll(".move-transcript-row").forEach((row) => {
      row.addEventListener("click", () => arenaJumpReplay(Number(row.dataset.plyIdx)));
    });

    app.querySelector("#replay-start")?.addEventListener("click", () => arenaJumpReplay(-1));
    app.querySelector("#replay-prev")?.addEventListener("click", () => arenaStepReplay(-1));
    app.querySelector("#replay-next")?.addEventListener("click", () => arenaStepReplay(1));
    app.querySelector("#replay-end")?.addEventListener("click", () => arenaJumpReplay(match.moves.length - 1));
    app.querySelector("#replay-autoplay")?.addEventListener("click", toggleArenaAutoPlay);
  } else {
    if (tbody) tbody.innerHTML = "";
    if (noMovesNote) {
      noMovesNote.style.display = "block";
      if (state === "QUEUED") {
        noMovesNote.textContent = "Match is queued and awaiting start. Plies will stream live once the game commences.";
      } else if (state === "STARTING") {
        noMovesNote.textContent = "Match is starting. Move recording will begin shortly.";
      } else if (state === "ABORTED" || state === "FAILED") {
        noMovesNote.textContent = `No moves were recorded before match termination (${match.failure_reason || "Terminated"}).`;
      } else {
        noMovesNote.textContent = "Move history is not available from the current API.";
      }
    }
  }

  // Technical Diagnostics
  const diagContent = app.querySelector("#arena-diagnostics-content");
  if (diagContent) {
    diagContent.innerHTML = `
      <div><strong>Match ID:</strong> <code>${esc(String(match.match_id))}</code></div>
      <div><strong>Match Seed:</strong> <code>${esc(String(match.match_seed || "Unavailable"))}</code></div>
      <div><strong>Rules Engine:</strong> <span>${esc(match.rules_version || "standard-chess/python-chess-1.11.2")}</span></div>
      <div><strong>Protocol:</strong> <span>${esc(match.protocol_version || "aik-jsonl/1")}</span></div>
      <div><strong>Runtime Profile:</strong> <span>${esc(match.runtime_profile || "python-3.12")}</span></div>
      <div><strong>Clock Setting:</strong> <span>${esc((match.initial_clock_ms || 300000) / 60000)}m + ${esc((match.increment_ms || 0) / 1000)}s</span></div>
      <div><strong>Move Timeout:</strong> <span>${esc(match.move_timeout_ms || 2000)} ms</span></div>
      <div><strong>Worker Isolation:</strong> <span>Fail-Closed Mock Subprocess</span></div>
    `;
  }

  // Lower section (Match history table)
  const lowerSection = app.querySelector("#arena-lower-section");
  if (lowerSection && matchHistoryList.length > 0) {
    lowerSection.innerHTML = `
      <div class="card-heading">
        <h2>${mode === "practice" ? "Recent Practice Matches" : "Official Tournament Matches"}</h2>
        <span class="muted-label">${matchHistoryList.length} matches recorded</span>
      </div>
      <div class="data-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Match</th>
              <th>${mode === "practice" ? "Opponent" : "White"}</th>
              <th>${mode === "practice" ? "Plies" : "Black"}</th>
              <th>Status</th>
              <th>Outcome</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${matchHistoryList.slice(0, 15).map((m) => `
              <tr class="${m.match_id === match.match_id ? "active-ply" : ""}">
                <td><code>${esc(String(m.match_id).slice(0, 8))}</code></td>
                <td><strong>${esc(mode === "practice" ? (m.house_ai?.house_ai_id === "house-ai-maximum-difficulty" ? "House AI (Max Difficulty)" : (m.house_ai?.house_ai_id || "House AI")) : (m.white_name || "White"))}</strong></td>
                <td>${mode === "practice" ? `${m.current_ply || 0} plies` : `<strong>${esc(m.black_name || "Black")}</strong>`}</td>
                <td>${badge(m.state)}</td>
                <td>${esc(m.result_outcome || m.result?.outcome || "In progress")}${m.state === "COMPLETED" ? `<small class="result-source">${resultSourceLabel(m.result_source)}</small>` : ""}</td>
                <td><button class="button button-secondary button-small" data-inspect-match="${esc(m.match_id)}" type="button">Inspect</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;

    lowerSection.querySelectorAll("[data-inspect-match]").forEach((btn) => {
      btn.addEventListener("click", () => loadGrandmasterArenaData(mode, btn.dataset.inspectMatch));
    });
  }

  // Live polling
  const livePulse = app.querySelector("#arena-live-pulse-badge");
  if (isRunning) {
    if (livePulse) {
      livePulse.className = "arena-live-indicator live";
      livePulse.innerHTML = state === "QUEUED"
        ? '<span class="live-dot"></span> LIVE SYNC (QUEUED)'
        : '<span class="live-dot"></span> LIVE SYNC (2s)';
    }
    arenaRefreshTimer = setTimeout(async () => {
      if (!app.querySelector("#arena-main-stage")) return;
      try {
        let freshMatch = null;
        if (mode === "practice" && sessionToken) {
          freshMatch = await api.developmentMatch(sessionToken, match.match_id);
        } else {
          freshMatch = await api.tournamentMatch(match.match_id);
        }
        if (freshMatch) {
          updateGrandmasterArena(freshMatch, mode, matchHistoryList);
        }
      } catch {
        if (livePulse) {
          livePulse.className = "arena-live-indicator idle";
          livePulse.innerHTML = '<span class="live-dot"></span> RECONNECTING…';
        }
        arenaRefreshTimer = setTimeout(() => {
          loadGrandmasterArenaData(mode, match.match_id);
        }, 5000);
      }
    }, 2000);
  } else {
    if (livePulse) {
      livePulse.className = "arena-live-indicator idle";
      livePulse.innerHTML = '<span class="live-dot"></span> MATCH TERMINAL';
    }
    if (mode === "spectator") {
      arenaRefreshTimer = setTimeout(() => {
        loadGrandmasterArenaData(mode, match.match_id);
      }, 15000);
    }
  }
}

function arenaJumpReplay(targetIdx) {
  if (!currentArenaMatchData || !Array.isArray(currentArenaMatchData.moves)) return;
  const moves = currentArenaMatchData.moves;
  currentArenaPlyIdx = Math.max(-1, Math.min(moves.length - 1, targetIdx));

  let fen = currentArenaMatchData.initial_fen || INITIAL_POSITION_FEN;
  let from = null;
  let to = null;
  if (currentArenaPlyIdx >= 0 && moves[currentArenaPlyIdx]) {
    const m = moves[currentArenaPlyIdx];
    fen = m.position_after_fen || fen;
    from = m.from_square;
    to = m.to_square;
  }

  const container = app.querySelector("#arena-board-container");
  if (container) {
    updateCinematicBoard(container, fen, { from, to, flipped: arenaBoardFlipped });
  }

  const stepLabel = app.querySelector("#replay-step-info");
  if (stepLabel) {
    stepLabel.textContent = currentArenaPlyIdx === -1 ? "Start Position" : `Ply ${currentArenaPlyIdx + 1} of ${moves.length}`;
  }

  const rows = app.querySelectorAll(".move-transcript-row");
  rows.forEach((row) => {
    const isActive = Number(row.dataset.plyIdx) === currentArenaPlyIdx;
    row.classList.toggle("active-ply", isActive);
    if (isActive && !isReducedMotionPreferred()) {
      row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  });
}

function arenaStepReplay(delta) {
  arenaJumpReplay(currentArenaPlyIdx + delta);
}

function toggleArenaAutoPlay() {
  const btn = app.querySelector("#replay-autoplay");
  if (arenaAutoPlayTimer) {
    clearInterval(arenaAutoPlayTimer);
    arenaAutoPlayTimer = null;
    if (btn) btn.textContent = "▶ Auto";
    return;
  }
  if (!currentArenaMatchData || !Array.isArray(currentArenaMatchData.moves)) return;
  const moves = currentArenaMatchData.moves;
  if (currentArenaPlyIdx >= moves.length - 1) {
    arenaJumpReplay(-1);
  }
  if (btn) btn.textContent = "⏸ Pause";
  arenaAutoPlayTimer = setInterval(() => {
    if (!app.querySelector("#arena-board-container") || currentArenaPlyIdx >= moves.length - 1) {
      clearInterval(arenaAutoPlayTimer);
      arenaAutoPlayTimer = null;
      if (btn) btn.textContent = "▶ Auto";
      return;
    }
    arenaStepReplay(1);
  }, 1000);
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const stage = document.getElementById("arena-main-stage");
    if (!stage || !stage.isConnected) return;
    if (!currentArenaMatchData || !Array.isArray(currentArenaMatchData.moves) || currentArenaMatchData.moves.length === 0) return;

    if (e.key === "ArrowLeft" || e.key === "k") {
      e.preventDefault();
      arenaStepReplay(-1);
    } else if (e.key === "ArrowRight" || e.key === "j") {
      e.preventDefault();
      arenaStepReplay(1);
    } else if (e.key === "Home") {
      e.preventDefault();
      arenaJumpReplay(-1);
    } else if (e.key === "End") {
      e.preventDefault();
      arenaJumpReplay(currentArenaMatchData.moves.length - 1);
    } else if (e.key === " " && !e.repeat) {
      e.preventDefault();
      toggleArenaAutoPlay();
    }
  });
}

async function loadArenaLive() {
  renderArena("spectator");
}

async function loadArenaBracket() {
  const target = app.querySelector("#arena-main");
  if (!target) return;
  try {
    const [swiss, playoffs] = await Promise.all([
      api.tournamentRounds("SWISS"),
      api.tournamentRounds("PLAYOFF"),
    ]);
    if (!target.isConnected) return;
    const renderRounds = (rounds, label) => rounds?.length ? rounds.map((round) => `<section class="round-section"><h3>${esc(round.round_name)}</h3><div class="data-table-wrap"><table class="data-table"><thead><tr><th>Pairing</th><th>Participant 1</th><th>Participant 2</th><th>Score</th><th>Status and result source</th></tr></thead><tbody>${(round.pairings || []).map((p) => { const sources = [...new Set([p.game1_result_source, p.game2_result_source, ...(p.sudden_death_result_sources || [])].filter(Boolean).map(resultSourceLabel))]; return `<tr><td>${p.is_bye ? "Bye" : `#${esc(p.pairing_index + 1)}`}</td><td><strong>${esc(p.participant1_name || "TBD")}</strong></td><td><strong>${esc(p.participant2_name || (p.is_bye ? "Bye" : "TBD"))}</strong></td><td>${esc(p.p1_points ?? "—")} – ${esc(p.p2_points ?? "—")}</td><td>${badge(p.status || round.status || "PENDING")}${sources.map((source) => `<small class="result-source">${esc(source)}</small>`).join("")}</td></tr>`; }).join("")}</tbody></table></div></section>`).join("") : `<p class="muted-copy">${label} have not been recorded yet.</p>`;
    target.innerHTML = `<section class="tournament-content"><span class="eyebrow">OFFICIAL COMPETITION</span><h1>Tournament</h1><section class="tournament-stage"><h2>Swiss standings rounds</h2>${renderRounds(swiss, "Swiss rounds")}</section><section class="tournament-stage"><h2>Playoff results</h2>${renderRounds(playoffs, "Playoff rounds")}</section></section>`;
  } catch {
    if (target.isConnected) target.innerHTML = `<section class="arena-unavailable"><span class="eyebrow">PUBLIC COMPETITION DATA</span><h1>Swiss Tournament Rounds</h1><p>Round data is currently unavailable.</p></section>`;
  }
}

async function loadArenaLeaderboard() {
  const target = app.querySelector("#arena-main");
  if (!target) return;
  try {
    const data = await api.tournamentLeaderboard();
    if (!target.isConnected) return;
    if (!data.standings || !data.standings.length) {
      target.innerHTML = `<section class="arena-unavailable"><span class="eyebrow">OFFICIAL COMPETITION</span><h1>Tournament Leaderboard</h1><p>Standings will update as tournament rounds conclude.</p></section>`;
      return;
    }
    const timestamp = data.updated_at ? dateTime(data.updated_at) : "Unavailable";
    target.innerHTML = `<section class="tournament-content leaderboard-snapshot"><span class="eyebrow">OFFICIAL SWISS STANDINGS</span><h1>Leaderboard</h1><p class="refresh-meta">Last successful refresh: <time>${esc(timestamp)}</time> <span id="leaderboard-refresh-state" aria-live="polite"></span><button class="link-button" type="button" id="refresh-leaderboard">Refresh</button></p><div class="data-table-wrap"><table class="data-table"><thead><tr><th>Rank</th><th>Participant / model</th><th>Points</th><th>Wins</th><th>Draws</th><th>Losses</th><th>Head to head</th><th>Buchholz</th></tr></thead><tbody>${data.standings.map((s) => `<tr><td><strong>#${esc(s.rank)}</strong></td><td><strong>${esc(s.name)}</strong>${s.team_name ? ` <small>(${esc(s.team_name)})</small>` : ""}</td><td><strong>${esc(s.points)}</strong></td><td>${esc(s.wins)}</td><td>${esc(s.draws)}</td><td>${esc(s.losses)}</td><td>${esc(s.head_to_head_points)}</td><td>${esc(s.opponent_strength)}</td></tr>`).join("")}</tbody></table></div></section>`;
    target.querySelector("#refresh-leaderboard")?.addEventListener("click", loadArenaLeaderboard);
  } catch {
    if (target.isConnected && target.querySelector(".leaderboard-snapshot")) {
      const state = target.querySelector("#leaderboard-refresh-state");
      if (state) state.textContent = "Refresh failed; displayed standings may be stale.";
    } else if (target.isConnected) target.innerHTML = `<section class="arena-unavailable"><span class="eyebrow">PUBLIC COMPETITION DATA</span><h1>Tournament Leaderboard</h1><p>Leaderboard data is currently unavailable.</p></section>`;
  }
}

function renderAdmin(active = "overview") {
  stopLandingAnimation();
  stopArenaRefresh();
  document.body.classList.remove("entry-page");
  document.body.classList.add("public-interface");
  route = active === "participants" ? "admin-participants" : "admin-matches";
  app.innerHTML = `
    <div class="admin-app">
      <header class="admin-header">
        <a class="brand" href="#admin" aria-label="AI Knockout Organizer Console">
          <span class="brand-mark">AI</span>
          <span class="brand-word">KNOCKOUT<small>TOURNAMENT CONTROL CENTER</small></span>
        </a>
        <div class="admin-status">
          <span><span class="status-badge warning" id="organizer-auth-state">CHECKING SESSION</span></span>
          <button type="button" class="button button-primary" id="organizer-login" hidden>Sign in as organizer</button>
          <button type="button" class="button button-secondary" id="organizer-logout" hidden>Sign out</button>
          <button type="button" class="button button-secondary" data-view="participant">Participant access</button>
        </div>
      </header>
      <div class="admin-layout">
        <aside class="admin-nav" aria-label="Organizer navigation">
          <button type="button" class="admin-nav-item ${active !== "participants" ? "active" : ""}" data-admin-page="matches" ${active !== "participants" ? 'aria-current="page"' : ""}>Matches</button>
          <button type="button" class="admin-nav-item ${active === "participants" ? "active" : ""}" data-admin-page="participants" ${active === "participants" ? 'aria-current="page"' : ""}>Participants</button>
        </aside>
        <main class="admin-main organizer-auth-blocked">
          ${active === "participants" ? `
            <section aria-labelledby="organizer-participants-heading">
              <div class="page-heading"><div><span class="eyebrow">TOURNAMENT OPERATIONS</span><h1 id="organizer-participants-heading">Participants</h1></div><div class="admin-data-actions"><span id="admin-data-state" class="admin-data-state" aria-live="polite">Checking organizer session…</span><button type="button" class="button button-secondary" id="admin-refresh">Refresh</button></div></div>
              <div id="admin-content" class="loading-copy">Loading participant administration…</div>
            </section>
          ` : `
            <section aria-labelledby="organizer-matches-heading">
              <div class="page-heading"><div><span class="eyebrow">TOURNAMENT OPERATIONS</span><h1 id="organizer-matches-heading">Matches</h1></div><div class="admin-data-actions"><span id="organizer-data-state" class="admin-data-state" aria-live="polite">Checking organizer session…</span><button type="button" class="button button-secondary" id="organizer-refresh">Refresh</button></div></div>
              <div id="organizer-match-management" class="loading-copy">Checking secure organizer session…</div>
            </section>
          `}
        </main>
      </div>
    </div>
  `;
  app.querySelectorAll("[data-admin-page]").forEach((btn) => {
    btn.addEventListener("click", () => renderAdmin(btn.dataset.adminPage));
  });
  app.querySelector("#organizer-refresh")?.addEventListener("click", loadPublicOrganizerMatches);
  app.querySelector("#admin-refresh")?.addEventListener("click", loadAdminParticipants);
  app.querySelector("#organizer-login")?.addEventListener("click", (event) => beginOrganizerLogin(event.currentTarget));
  app.querySelector("#organizer-logout")?.addEventListener("click", async () => {
    if (!organizerCsrfToken) return;
    try { await api.organizerLogout(organizerCsrfToken); }
    catch (error) { if (error.status !== 401) notify(error.message || "Could not sign out."); }
    organizerCsrfToken = null;
    organizerProfile = null;
    renderAdmin(active === "participants" ? "participants" : "matches");
  });
  app.querySelectorAll('[data-view="participant"]').forEach((btn) => btn.addEventListener("click", returnToParticipantAccess));
  if (active === "participants") {
    loadAdminParticipants();
  } else {
    loadPublicOrganizerMatches();
  }
}

async function beginOrganizerLogin(button) {
  button.disabled = true;
  button.textContent = "Connecting to identity provider…";
  try {
    const data = await api.organizerLogin();
    const authorizationUrl = new URL(data?.authorization_url || "");
    const loopbackHttp = authorizationUrl.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(authorizationUrl.hostname);
    if (authorizationUrl.protocol !== "https:" && !loopbackHttp) throw new ApiError(0, "The organizer identity provider URL is not secure.");
    window.location.assign(authorizationUrl.href);
  } catch (error) {
    button.disabled = false;
    button.textContent = "Sign in as organizer";
    const target = app.querySelector("#organizer-match-management");
    if (!target) return;
    const message = error.status === 503
      ? "Organizer sign-in is not configured on this local server. Contact the project operator."
      : error.message || "Could not start organizer sign-in. Try again.";
    target.innerHTML = renderOrganizerMatchManagement([], { integrationAvailable: false, loadError: message });
  }
}

async function loadPublicOrganizerMatches() {
  const target = app.querySelector("#organizer-match-management");
  if (!target) return;
  const state = app.querySelector("#organizer-data-state");
  const authBadge = app.querySelector("#organizer-auth-state");
  const login = app.querySelector("#organizer-login");
  const logout = app.querySelector("#organizer-logout");
  try {
    const profile = await api.organizerSession();
    if (!profile?.csrf_token || !profile?.organizer_email || !Number.isFinite(Date.parse(profile.expires_at))) {
      throw new ApiError(0, "The organizer session response did not match the server identity, CSRF, and expiry contract.");
    }
    organizerProfile = profile;
    organizerCsrfToken = profile.csrf_token;
    if (authBadge) { authBadge.textContent = "ORGANIZER SESSION ACTIVE"; authBadge.className = "status-badge success"; }
    if (logout) logout.hidden = false;
    if (login) login.hidden = true;
    if (state) state.textContent = `Signed in as ${profile.organizer_name || profile.organizer_email}`;
    const response = await api.tournamentMatches();
    if (!target.isConnected) return;
    const matches = Array.isArray(response) ? response : (Array.isArray(response?.items) ? response.items : []);
    const adapter = {
      recordResult: async (matchId, body) => {
        const current = await api.organizerSession();
        if (!current?.csrf_token || !current?.organizer_email || !Number.isFinite(Date.parse(current.expires_at))) {
          throw new ApiError(401, "Organizer session expired or did not match the server session contract. Sign in again.");
        }
        organizerCsrfToken = current.csrf_token;
        return api.adminRecordMatchResult(matchId, body, organizerCsrfToken);
      },
      getMatches: async () => api.tournamentMatches(),
      getMatch: (matchId) => api.tournamentMatch(matchId),
      getRounds: (stage) => api.tournamentRounds(stage),
      getLeaderboard: () => api.tournamentLeaderboard(),
      getBracket: () => api.tournamentBracket(),
      getAuditLog: () => api.adminAuditLog(50),
    };
    let auditEvents = null;
    let auditError = "";
    try { auditEvents = await api.adminAuditLog(50); }
    catch (error) {
      if (error.status === 401 || error.status === 403) throw error;
      auditError = error.message || "Could not load organizer audit history.";
    }
    if (!target.isConnected) return;
    target.innerHTML = renderOrganizerMatchManagement(matches, { integrationAvailable: true, organizerName: profile.organizer_name || profile.organizer_email, auditEvents, auditError });
    wireOrganizerMatchManagement(target, {
      api: adapter,
      onUnauthorized: () => { organizerCsrfToken = null; organizerProfile = null; renderAdmin("matches"); },
      onRefresh: async (data) => {
        const refreshed = Array.isArray(data.matches) ? data.matches : data.matches?.items;
        const currentAudit = data.auditEvents || auditEvents;
        const currentAuditError = data.refreshErrors?.auditEvents ? "Audit refresh failed after result recording." : "";
        target.innerHTML = renderOrganizerMatchManagement(refreshed || matches, {
          integrationAvailable: true,
          organizerName: organizerProfile?.organizer_name || organizerProfile?.organizer_email,
          auditEvents: currentAudit,
          auditError: currentAuditError,
          refreshSnapshot: renderOrganizerRefreshSnapshot(data),
        });
        wireOrganizerMatchManagement(target, { api: adapter, onUnauthorized: () => { organizerCsrfToken = null; organizerProfile = null; renderAdmin("matches"); } });
      },
    });
  } catch (error) {
    if (!target.isConnected) return;
    organizerCsrfToken = null;
    organizerProfile = null;
    if (authBadge) { authBadge.textContent = error.status === 401 ? "SIGN IN REQUIRED" : "AUTHENTICATION UNAVAILABLE"; authBadge.className = "status-badge warning"; }
    if (logout) logout.hidden = true;
    if (login) login.hidden = error.status !== 401;
    if (state) state.textContent = error.status === 401 ? "No active organizer session" : "Session check failed";
    const message = error.status === 401
      ? "No active organizer session. Sign in with your approved organizer identity to continue; organizer actions remain disabled until the server confirms the session."
      : error.status === 403
        ? "Organizer authorization or CSRF verification failed. No organizer controls are enabled."
        : error.message || "Organizer session could not be verified. No organizer controls are enabled.";
    target.innerHTML = renderOrganizerMatchManagement([], { integrationAvailable: false, loadError: message });
  }
}

function refreshAdminSection(section) {
  if (section === "participants") loadAdminParticipants();
  else if (section === "submissions") loadAdminSubmissions();
  else if (section === "allowances") loadAdminAllowances();
  else if (section === "controls") loadAdminControls();
  else if (section === "audit") loadAdminAudit();
  else loadAdminOverview();
}

function markAdminFresh(target) {
  target.dataset.loaded = "true";
  const state = app.querySelector("#admin-data-state");
  if (!state) return;
  state.dataset.lastSuccess = new Date().toISOString();
  state.textContent = `Updated ${dateTime(state.dataset.lastSuccess)}`;
  state.classList.remove("is-stale");
}

function markAdminStale() {
  const state = app.querySelector("#admin-data-state");
  if (!state) return;
  state.classList.add("is-stale");
  state.textContent = state.dataset.lastSuccess
    ? `Refresh failed · last successful update ${dateTime(state.dataset.lastSuccess)} · data may be stale`
    : "Current data is unavailable";
}

function handleAdminError(target, err) {
  if (!target || !target.isConnected) return;
  if (err.status === 401 || err.status === 403) {
    organizerCsrfToken = null;
    organizerProfile = null;
    notify("Organizer session expired or invalid. Please re-authenticate.");
    renderAdmin("overview");
    return;
  }
  markAdminStale();
  if (target.dataset.loaded === "true") return;
  target.innerHTML = `
    <div style="padding: 20px; border: 1px solid #edc6c6; border-left: 3px solid #dc4d4d; background: #fff; border-radius: 4px;">
      <strong style="color: #842d2d;">Error loading admin data</strong>
      <p style="margin: 8px 0 0; color: #5d6878; font-size: 12px;">${esc(err.message || "An unexpected error occurred.")}</p>
    </div>
  `;
}

async function loadAdminOverview() {
  const target = app.querySelector("#admin-content");
  if (!target) return;
  try {
    const data = await api.adminOverview();
    if (!target.isConnected) return;
    markAdminFresh(target);
    target.innerHTML = `
      <div style="margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; background: #fff; border: 1px solid #dce2e9; border-radius: 6px;">
        <div>
          <strong style="font-size: 13px; color: #17233b;">Official Submissions Status: </strong>
          <span style="font-size: 13px; color: #5d6878;">${data.official_frozen ? "FROZEN (Locked for competition)" : "OPEN (Participants may update official selections)"}</span>
        </div>
        <div>
          ${data.official_frozen ? '<span class="status-badge danger">OFFICIAL FROZEN</span>' : '<span class="status-badge success">OFFICIAL OPEN</span>'}
        </div>
      </div>
      <section class="admin-empty-grid">
        <article><span>Registered Participants</span><strong>${data.participants_count}</strong><small>Total registered accounts</small></article>
        <article><span>Submissions Total</span><strong>${data.submissions_count}</strong><small>Participant submissions</small></article>
        <article><span>Submission Versions</span><strong>${data.submission_versions_count}</strong><small>Total versions uploaded</small></article>
        <article><span>Eligible Versions</span><strong>${data.eligible_versions_count}</strong><small>Approved for tournament</small></article>
        <article><span>Official Selections</span><strong>${data.official_selections_count}</strong><small>Participants with active selection</small></article>
        <article><span>Official Freeze</span><strong>${data.official_frozen ? "LOCKED" : "OPEN"}</strong><small>${data.official_freeze_at ? esc(dateTime(data.official_freeze_at)) : "Awaiting organizer freeze"}</small></article>
        <article><span>Matches Total</span><strong>${data.matches_total}</strong><small>Total matches recorded</small></article>
        <article><span>Active Matches</span><strong>${data.matches_active}</strong><small>Running / starting / queued</small></article>
        <article><span>Completed Matches</span><strong>${data.matches_completed}</strong><small>Concluded tournament matches</small></article>
      </section>
    `;
  } catch (err) {
    handleAdminError(target, err);
  }
}

function renderAdminParticipantsTableHtml(list) {
  if (!Array.isArray(list) || !list.length) {
    return `<section class="admin-empty-state"><h2>No participants registered</h2><p>Participant accounts will appear here after creation.</p></section>`;
  }
  return `
    <div class="data-table-wrap admin-table-wrap"><table class="data-table admin-participants-table">
      <thead><tr><th>Participant</th><th>Status</th><th>Submissions</th><th>Quota</th><th>Official entry</th><th>Details & actions</th></tr></thead>
      <tbody>${list.map((p) => {
        const search = [p.participant_id, p.name, p.team_name, p.email].filter(Boolean).join(" ");
        const selection = p.official_submission_version_id ? `Version ${p.official_submission_version_id}` : "No official version selected";
        return `<tr data-participant-row data-search="${esc(search.toLowerCase())}">
          <td><strong>${esc(p.name)}</strong><small class="admin-participant-id"><code>${esc(p.participant_id)}</code></small></td>
          <td>${badge(p.status)}</td>
          <td>${esc(p.submissions_count ?? 0)}</td>
          <td><strong>${esc(p.accepted_submissions_count ?? 0)} / ${esc(p.submission_limit ?? 10)}</strong><small>${esc(p.remaining_submissions ?? 0)} remaining</small><button class="table-action" type="button" data-adjust-quota="${esc(p.participant_id)}" data-pname="${esc(p.name)}" data-limit="${esc(p.submission_limit ?? 10)}">Adjust quota</button></td>
          <td>${p.has_official_selection ? '<span class="status-badge success">Selected</span>' : '<span class="status-badge neutral">None</span>'}</td>
          <td><details class="participant-manage"><summary>Manage</summary><div class="participant-detail"><dl><div><dt>Participant ID</dt><dd><code>${esc(p.participant_id)}</code></dd></div><div><dt>Team</dt><dd>${esc(p.team_name || "—")}</dd></div><div><dt>Email</dt><dd>${esc(p.email || "—")}</dd></div><div><dt>Registered</dt><dd>${esc(dateTime(p.created_at))}</dd></div><div><dt>Sessions recorded</dt><dd>${esc(p.sessions_count ?? 0)}</dd></div><div><dt>Official entry</dt><dd>${esc(selection)}</dd></div></dl><form class="participant-action-form" data-participant-id="${esc(p.participant_id)}" data-participant-name="${esc(p.name)}"><label>Administrative action<select name="action" required><option value="suspend">Suspend and revoke sessions</option><option value="reinstate">Reinstate participant</option><option value="revoke-sessions">Revoke active sessions</option><option value="remove">Remove unused account</option></select></label><label>Reason <textarea name="reason" maxlength="500" required placeholder="Record the reason for this action"></textarea></label><p class="participant-action-note">Removal is limited to accounts without submissions or tournament records; historical results are preserved.</p><button class="button button-secondary" type="submit">Confirm action</button></form></div></details></td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>
  `;
}

function wireAdminParticipantsTableControls(target) {
  const searchInput = target.querySelector("#participant-search");
  searchInput?.addEventListener("input", () => {
    const term = searchInput.value.trim().toLowerCase();
    target.querySelectorAll("[data-participant-row]").forEach((row) => { row.hidden = term && !row.dataset.search.includes(term); });
  });

  target.querySelectorAll(".participant-action-form").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const participantId = form.dataset.participantId;
    const participantName = form.dataset.participantName;
    const actionControl = form.querySelector('[name="action"]');
    const reasonControl = form.querySelector('[name="reason"]');
    const action = actionControl.value;
    const reason = reasonControl.value.trim();
    const labels = { suspend: "suspend and revoke sessions for", reinstate: "reinstate", "revoke-sessions": "revoke active sessions for", remove: "remove the unused account for" };
    if (!reason) { reasonControl.focus(); return; }
    const confirmation = `Confirm ${labels[action]} ${participantName}?\n\nReason: ${reason}`;
    if (!window.confirm(confirmation)) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = "Applying…";
    try {
      const payload = { reason, actor_id: "organizer-admin" };
      const result = action === "remove"
        ? await api.adminRemoveParticipant(participantId, payload, organizerCsrfToken)
        : await api.adminParticipantAction(participantId, action, payload, organizerCsrfToken);
      if (!result?.audited) throw new Error("The server did not confirm an audited outcome. Refresh participant data before retrying.");
      const outcome = action === "remove" ? "Account removed" : action === "suspend" ? `Participant suspended; ${result.revoked_sessions ?? 0} sessions revoked` : action === "reinstate" ? "Participant reinstated" : `${result.revoked_sessions ?? 0} active sessions revoked`;
      notify(`${outcome}: ${participantName}.`);
      await refreshAdminParticipantsTable(target);
    } catch (err) {
      notify(err.message || "The server did not confirm the administrative action.");
      button.disabled = false;
      button.textContent = "Confirm action";
    }
  }));

  target.querySelectorAll("[data-adjust-quota]").forEach((btn) => btn.addEventListener("click", async () => {
    const participantId = btn.dataset.adjustQuota;
    const participantName = btn.dataset.pname;
    const currentLimit = btn.dataset.limit;
    const newLimitStr = prompt(`Set new submission limit for ${participantName} (current: ${currentLimit}):`, currentLimit);
    if (!newLimitStr) return;
    const newLimit = Number.parseInt(newLimitStr.trim(), 10);
    if (!Number.isInteger(newLimit) || newLimit < 1) { alert("Submission limit must be a positive integer."); return; }
    const reason = prompt(`Reason for quota exception for ${participantName}:`);
    if (!reason || !reason.trim()) { alert("Reason is required."); return; }
    if (!window.confirm(`Confirm quota change for ${participantName} to ${newLimit}?\n\nReason: ${reason.trim()}`)) return;
    btn.disabled = true;
    btn.textContent = "Updating…";
    try {
      const result = await api.adminUpdateParticipantAllowance(participantId, { submission_limit: newLimit, reason: reason.trim(), actor_id: "organizer-admin" }, organizerCsrfToken);
      if (!result?.audited && !result?.participant_id) throw new Error("The server did not confirm the quota update.");
      notify(`Quota updated for ${participantName}.`);
      await refreshAdminParticipantsTable(target);
    } catch (err) {
      notify(err.message || "Failed to update quota.");
      btn.disabled = false;
      btn.textContent = "Adjust quota";
    }
  }));
}

async function refreshAdminParticipantsTable(target) {
  try {
    const list = await api.adminParticipants();
    const container = target.querySelector("#admin-participants-table-container");
    if (container) {
      container.innerHTML = renderAdminParticipantsTableHtml(list);
      wireAdminParticipantsTableControls(target);
    }
  } catch (err) {
    notify(err.message || "Could not refresh participant table.");
  }
}

function wireAdminCreateParticipant(target) {
  const form = target.querySelector("#admin-create-participant-form");
  if (!form) return;
  const nameInput = target.querySelector("#admin-reg-name");
  const teamInput = target.querySelector("#admin-reg-team");
  const emailInput = target.querySelector("#admin-reg-email");
  const errP = target.querySelector("#admin-create-error");
  const submitBtn = target.querySelector("#admin-create-submit");
  const credSlot = target.querySelector("#admin-credential-slot");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = (nameInput.value || "").trim();
    const team_name = (teamInput.value || "").trim();
    const email = (emailInput.value || "").trim();
    errP.textContent = "";

    if (name.length < 2) {
      errP.textContent = "Participant name must be at least 2 characters.";
      nameInput.focus();
      return;
    }
    if (name.length > 100) {
      errP.textContent = "Participant name exceeds 100 characters.";
      nameInput.focus();
      return;
    }
    if (/[\x00-\x1f<>]/.test(name)) {
      errP.textContent = "Participant name contains prohibited characters.";
      nameInput.focus();
      return;
    }
    if (team_name && team_name.length > 100) {
      errP.textContent = "Team name exceeds 100 characters.";
      teamInput.focus();
      return;
    }
    if (team_name && /[\x00-\x1f<>]/.test(team_name)) {
      errP.textContent = "Team name contains prohibited characters.";
      teamInput.focus();
      return;
    }
    if (email && !/^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/.test(email)) {
      errP.textContent = "Please enter a valid email address.";
      emailInput.focus();
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "Issuing credentials…";

    try {
      const current = await api.organizerSession();
      if (!current?.csrf_token || !current?.organizer_email || !Number.isFinite(Date.parse(current.expires_at))) {
        throw new ApiError(401, "Organizer session expired or invalid. Sign in again.");
      }
      organizerCsrfToken = current.csrf_token;
      organizerProfile = current;

      const idempotencyKey = typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `org-reg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

      const payload = {
        name,
        team_name: team_name || undefined,
        email: email || undefined,
      };

      const result = await api.organizerRegisterParticipant(payload, organizerCsrfToken, idempotencyKey);

      credSlot.innerHTML = `
        <div class="organizer-credential-card" id="admin-credential-receipt" role="region" aria-label="Issued Participant Credentials">
          <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px;">
            <div>
              <span class="status-badge success" style="font-size: 11px;">CREDENTIALS ISSUED</span>
              <h3 style="margin: 6px 0 2px; font-size: 16px; color: var(--ink);">Participant: <strong>${esc(result.name)}</strong></h3>
              <p style="margin: 0; font-size: 12px; color: var(--muted);">${result.team_name ? `Team: ${esc(result.team_name)} &middot; ` : ""}Status: ${esc(result.status)}</p>
            </div>
            <button type="button" class="button button-secondary" id="admin-dismiss-cred" style="padding: 4px 8px; font-size: 11px;">Dismiss</button>
          </div>
          <p style="margin: 0 0 12px; font-size: 12px; color: var(--ink-soft); line-height: 1.45;">
            Issued credentials are displayed <strong>once</strong>. Store or transmit these credentials to the participant securely before leaving this view.
          </p>
          <div style="display: grid; gap: 8px; margin-bottom: 12px;">
            <div style="display: flex; align-items: center; justify-content: space-between; background: var(--surface); padding: 8px 12px; border: 1px solid var(--line); border-radius: 4px;">
              <div>
                <span style="font-size: 10px; color: var(--muted); display: block;">PARTICIPANT ID</span>
                <code id="admin-cred-id" style="font-size: 13px; font-weight: 600;">${esc(result.participant_id)}</code>
              </div>
              <button type="button" class="button button-secondary" id="admin-copy-id" style="padding: 4px 10px; font-size: 11px;">Copy ID</button>
            </div>
            <div style="display: flex; align-items: center; justify-content: space-between; background: var(--surface); padding: 8px 12px; border: 1px solid var(--line); border-radius: 4px;">
              <div>
                <span style="font-size: 10px; color: var(--muted); display: block;">ACCESS KEY (CONFIDENTIAL &middot; ONE-TIME DISPLAY)</span>
                <code id="admin-cred-key" class="cred-secret" style="font-size: 13px;">${esc(result.access_key)}</code>
              </div>
              <button type="button" class="button button-secondary" id="admin-copy-key" style="padding: 4px 10px; font-size: 11px;">Copy Key</button>
            </div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <button type="button" class="button button-secondary" id="admin-copy-both" style="padding: 6px 12px; font-size: 12px;">Copy Both</button>
            <button type="button" class="button button-primary" id="admin-signin-participant" style="padding: 6px 12px; font-size: 12px;">Sign In as this Participant</button>
            <button type="button" class="button button-secondary" id="admin-create-another" style="padding: 6px 12px; font-size: 12px;">Create Another</button>
            <span id="admin-cred-copy-status" class="copy-status" role="status" aria-live="polite" style="font-size: 12px; color: var(--accent);"></span>
          </div>
        </div>
      `;

      const statusEl = credSlot.querySelector("#admin-cred-copy-status");
      const writeClipboard = async (text, msg) => {
        try {
          if (typeof navigator !== "undefined" && navigator?.clipboard?.writeText) {
            await copyCredentialValue(text, (t) => navigator.clipboard.writeText(t));
          }
          if (statusEl) statusEl.textContent = msg;
          notify(msg);
        } catch {
          if (statusEl) statusEl.textContent = "Could not copy automatically. Please select and copy manually.";
        }
      };

      credSlot.querySelector("#admin-copy-id")?.addEventListener("click", () => writeClipboard(result.participant_id, "Participant ID copied to clipboard."));
      credSlot.querySelector("#admin-copy-key")?.addEventListener("click", () => writeClipboard(result.access_key, "Access Key copied to clipboard."));
      credSlot.querySelector("#admin-copy-both")?.addEventListener("click", () => {
        const combined = credentialsText(result.participant_id, result.access_key);
        writeClipboard(combined, "Both credentials copied to clipboard.");
      });
      credSlot.querySelector("#admin-signin-participant")?.addEventListener("click", () => {
        landing("login", "", { participant_id: result.participant_id, access_key: result.access_key });
      });
      credSlot.querySelector("#admin-create-another")?.addEventListener("click", () => {
        credSlot.innerHTML = "";
        nameInput.focus();
      });
      credSlot.querySelector("#admin-dismiss-cred")?.addEventListener("click", () => {
        credSlot.innerHTML = "";
      });

      form.reset();
      notify(`Participant created: ${result.name} (${result.participant_id})`);
      await refreshAdminParticipantsTable(target);
    } catch (error) {
      errP.textContent = error.message || "Failed to create participant.";
      if (error.status === 401 || error.status === 403) {
        notify("Organizer session expired or invalid. Please re-authenticate.");
        loadAdminParticipants();
      }
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Create Participant";
    }
  });
}

async function loadAdminParticipants() {
  const target = app.querySelector("#admin-content");
  if (!target) return;
  const authBadge = app.querySelector("#organizer-auth-state");
  const loginBtn = app.querySelector("#organizer-login");
  const logoutBtn = app.querySelector("#organizer-logout");
  const dataState = app.querySelector("#admin-data-state");

  try {
    const profile = await api.organizerSession();
    if (!profile?.csrf_token || !profile?.organizer_email || !Number.isFinite(Date.parse(profile.expires_at))) {
      throw new ApiError(0, "The organizer session response did not match the server session contract.");
    }
    organizerProfile = profile;
    organizerCsrfToken = profile.csrf_token;
    if (authBadge) { authBadge.textContent = "ORGANIZER SESSION ACTIVE"; authBadge.className = "status-badge success"; }
    if (logoutBtn) logoutBtn.hidden = false;
    if (loginBtn) loginBtn.hidden = true;
    if (dataState) dataState.textContent = `Signed in as ${profile.organizer_name || profile.organizer_email}`;
  } catch (err) {
    organizerCsrfToken = null;
    organizerProfile = null;
    if (authBadge) { authBadge.textContent = err.status === 401 ? "SIGN IN REQUIRED" : "AUTHENTICATION UNAVAILABLE"; authBadge.className = "status-badge warning"; }
    if (logoutBtn) logoutBtn.hidden = true;
    if (loginBtn) loginBtn.hidden = err.status !== 401;
    if (dataState) dataState.textContent = err.status === 401 ? "No active organizer session" : "Session check failed";
    target.innerHTML = `
      <section class="admin-empty" style="border-left-color: var(--warning);">
        <strong>Organizer Authentication Required</strong>
        <p>Participant creation and management require an active organizer session. Sign in with your approved organizer identity to continue.</p>
        <div style="margin-top: 16px; max-width: 400px; background: var(--surface); padding: 16px; border: 1px solid var(--line); border-radius: 4px;">
          <h3 style="margin: 0 0 10px; font-size: 14px; color: var(--ink);">Sign In with Organizer Credentials</h3>
          <form id="organizer-participant-login-form">
            <div class="field" style="margin-bottom: 10px;">
              <label for="org-login-email" style="font-size: 11px; font-weight: 600; display: block; margin-bottom: 4px;">Organizer Email</label>
              <input class="input" id="org-login-email" type="email" placeholder="organizer@tournament.local" required style="width: 100%;">
            </div>
            <div class="field" style="margin-bottom: 12px;">
              <label for="org-login-password" style="font-size: 11px; font-weight: 600; display: block; margin-bottom: 4px;">Organizer Password</label>
              <input class="input" id="org-login-password" type="password" placeholder="••••••••••••" required style="width: 100%;">
            </div>
            <p id="org-login-error" class="form-message" role="alert" style="color: var(--danger, #dc2626); font-size: 12px; margin-bottom: 8px;"></p>
            <div style="display: flex; gap: 8px;">
              <button type="submit" class="button button-primary" id="btn-org-login-submit">Sign in as Organizer</button>
              <button type="button" class="button button-secondary" id="btn-org-oidc-login">Use IdP Login</button>
            </div>
          </form>
        </div>
      </section>
    `;
    const form = target.querySelector("#organizer-participant-login-form");
    form?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const email = target.querySelector("#org-login-email").value.trim();
      const password = target.querySelector("#org-login-password").value;
      const errP = target.querySelector("#org-login-error");
      const submitBtn = target.querySelector("#btn-org-login-submit");
      submitBtn.disabled = true;
      submitBtn.textContent = "Signing in…";
      errP.textContent = "";
      try {
        await api.organizerLoginPassword({ email, password });
        notify("Organizer session confirmed.");
        loadAdminParticipants();
      } catch (loginErr) {
        errP.textContent = loginErr.message || "Invalid organizer credentials.";
        submitBtn.disabled = false;
        submitBtn.textContent = "Sign in as Organizer";
      }
    });
    target.querySelector("#btn-org-oidc-login")?.addEventListener("click", (e) => beginOrganizerLogin(e.currentTarget));
    return;
  }

  try {
    const list = await api.adminParticipants();
    if (!target.isConnected) return;
    markAdminFresh(target);

    target.innerHTML = `
      <section class="organizer-reg-details" aria-labelledby="create-participant-heading">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
          <div>
            <span class="eyebrow" style="font-size: 10px; color: var(--muted); letter-spacing: 0.05em; font-weight: 700;">ORGANIZER PARTICIPANT AUTHORIZATION</span>
            <h2 id="create-participant-heading" style="margin: 4px 0 0; font-size: 17px; color: var(--ink);">Create Participant</h2>
          </div>
          <span class="status-badge success" style="font-size: 10px;">AUTHORIZED CONSOLE</span>
        </div>
        <p style="margin: 0 0 12px; font-size: 13px; color: var(--ink-soft); line-height: 1.5;">
          Register and issue credentials for an authorized tournament participant. Participant ID and Access Key are generated securely on the server.
        </p>
        <div id="admin-credential-slot"></div>
        <form id="admin-create-participant-form">
          <div class="form-grid-3">
            <div class="field">
              <label for="admin-reg-name">Participant Name <span style="color: var(--muted); font-size: 11px;">Required (2–100 chars)</span></label>
              <input class="input" id="admin-reg-name" name="name" autocomplete="off" placeholder="e.g. DeepKnight AI" minlength="2" maxlength="100" required>
            </div>
            <div class="field">
              <label for="admin-reg-team">Team Name <span style="color: var(--muted); font-size: 11px;">Optional</span></label>
              <input class="input" id="admin-reg-team" name="team_name" autocomplete="off" placeholder="e.g. Team Alpha" maxlength="100">
            </div>
            <div class="field">
              <label for="admin-reg-email">Contact Email <span style="color: var(--muted); font-size: 11px;">Optional</span></label>
              <input class="input" id="admin-reg-email" name="email" type="email" autocomplete="email" placeholder="e.g. team@college.edu" maxlength="255">
            </div>
          </div>
          <p id="admin-create-error" class="form-message" role="alert" style="color: var(--danger, #dc2626); margin-top: 6px; font-size: 12px;"></p>
          <div style="display: flex; gap: 10px; align-items: center; margin-top: 10px;">
            <button class="button button-primary" type="submit" id="admin-create-submit">Create Participant</button>
          </div>
        </form>
      </section>

      <div class="admin-participant-tools" style="margin-top: 24px;">
        <label for="participant-search">Search participants</label>
        <input id="participant-search" class="input" type="search" placeholder="Name, participant ID, team, or email" autocomplete="off">
      </div>
      <div id="admin-participants-table-container">
        ${renderAdminParticipantsTableHtml(list)}
      </div>
    `;

    wireAdminCreateParticipant(target);
    wireAdminParticipantsTableControls(target);
  } catch (err) {
    handleAdminError(target, err);
  }
}

async function loadAdminSubmissions() {
  const target = app.querySelector("#admin-content");
  if (!target) return;
  try {
    const list = await api.adminSubmissions();
    if (!target.isConnected) return;
    markAdminFresh(target);
    if (!list || !list.length) {
      target.innerHTML = `<div style="padding: 24px; background: #fff; border: 1px solid #dce2e9; border-radius: 6px;"><p>No submissions uploaded yet.</p></div>`;
      return;
    }
    target.innerHTML = `
      <div class="data-table-wrap" style="background: #fff; border: 1px solid #dce2e9; border-radius: 6px; padding: 12px;">
        <table class="data-table">
          <thead>
            <tr>
              <th>Participant</th>
              <th>Version ID</th>
              <th>Ver</th>
              <th>State</th>
              <th>Validation</th>
              <th>Digest (SHA-256)</th>
              <th>Created</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${list.map((s) => `
              <tr>
                <td><strong>${esc(s.participant_name)}</strong><br><small style="color:#718096;"><code>${esc(s.participant_id)}</code></small></td>
                <td><code>${esc(s.version_id ? s.version_id.slice(0, 8) : "—")}</code></td>
                <td>v${esc(s.version)}</td>
                <td>${badge(s.state)}</td>
                <td>${s.validation_outcome ? badge(s.validation_outcome) : '<span class="status-badge neutral">PENDING</span>'}</td>
                <td title="${esc(s.artifact_sha256 || "")}"><code>${esc(s.artifact_sha256 ? s.artifact_sha256.slice(0, 12) + "…" : "—")}</code></td>
                <td>${esc(dateTime(s.created_at))}</td>
                <td>
                  ${s.state !== "ELIGIBLE" ? `
                    <button type="button" class="button button-secondary" style="padding: 4px 8px; font-size: 11px;" data-approve-version="${esc(s.version_id)}" data-participant="${esc(s.participant_name)}" data-version="${esc(s.version)}">Approve Eligibility</button>
                  ` : '<span class="status-badge success">ELIGIBLE</span>'}
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
    target.querySelectorAll("[data-approve-version]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const versionId = btn.dataset.approveVersion;
        const participantName = btn.dataset.participant;
        const ver = btn.dataset.version;
        if (!confirm(`Approve submission v${ver} for ${participantName} as tournament ELIGIBLE?`)) return;
        btn.disabled = true;
        btn.textContent = "Approving…";
        try {
          await api.adminApproveSubmission({
            submission_version_id: versionId,
            reviewer_id: "organizer-admin",
            notes: "Approved via Organizer Control Center",
          });
          notify(`Version ${ver} for ${participantName} approved as ELIGIBLE.`);
          loadAdminSubmissions();
        } catch (err) {
          notify(err.message || "Failed to approve submission.");
          btn.disabled = false;
          btn.textContent = "Approve Eligibility";
        }
      });
    });
  } catch (err) {
    handleAdminError(target, err);
  }
}

async function loadAdminAllowances() {
  const target = app.querySelector("#admin-content");
  if (!target) return;
  try {
    const list = await api.adminAllowanceRequests();
    if (!target.isConnected) return;
    markAdminFresh(target);
    if (!list || !list.length) {
      target.innerHTML = `
        <div style="padding: 24px; background: #fff; border: 1px solid #dce2e9; border-radius: 6px;">
          <h2 style="font-size: 15px; margin: 0 0 8px; color: #17233b;">Quota Exception Requests</h2>
          <p style="color: #5d6878; font-size: 13px;">No quota increase requests have been submitted by participants.</p>
        </div>
      `;
      return;
    }
    target.innerHTML = `
      <div style="background: #fff; border: 1px solid #dce2e9; border-radius: 6px; padding: 16px;">
        <h2 style="font-size: 15px; margin: 0 0 12px; color: #17233b;">Participant Quota Increase Requests</h2>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>Participant</th>
                <th>Current Quota</th>
                <th>Requested Limit</th>
                <th>Justification</th>
                <th>Status</th>
                <th>Submitted</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${list.map((r) => `
                <tr>
                  <td>
                    <strong>${esc(r.participant_name)}</strong><br>
                    <code>${esc(r.participant_id)}</code>
                  </td>
                  <td>${r.accepted_count} / ${r.current_limit}</td>
                  <td><strong>${r.requested_limit}</strong></td>
                  <td style="max-width: 250px; word-break: break-word;">${esc(r.reason)}</td>
                  <td>${badge(r.status)}</td>
                  <td>${esc(dateTime(r.created_at))}</td>
                  <td>
                    ${r.status === "PENDING" ? `
                      <div style="display: flex; gap: 6px;">
                        <button class="table-action" style="color: #198754;" type="button" data-decide-req="${esc(r.id)}" data-decision="APPROVED" data-pname="${esc(r.participant_name)}" data-limit="${r.requested_limit}">Approve</button>
                        <button class="table-action" style="color: #dc3545;" type="button" data-decide-req="${esc(r.id)}" data-decision="REJECTED" data-pname="${esc(r.participant_name)}">Reject</button>
                      </div>
                    ` : `
                      <small style="color: #6c757d;">${esc(r.reviewed_by || "Reviewed")}<br>${r.review_notes ? esc(r.review_notes) : ""}</small>
                    `}
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    `;

    target.querySelectorAll("[data-decide-req]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const reqId = btn.dataset.decideReq;
        const decision = btn.dataset.decision;
        const participantName = btn.dataset.pname;
        const requestedLimit = btn.dataset.limit;
        const confirmMsg = decision === "APPROVED"
          ? `Approve quota increase for ${participantName} to ${requestedLimit} submissions?`
          : `Reject quota increase request for ${participantName}?`;
        if (!confirm(confirmMsg)) return;
        const notes = prompt(`Notes / rationale for ${decision.toLowerCase()}ing this request:`, `${decision} by organizer`);
        btn.disabled = true;
        btn.textContent = "Processing…";
        try {
          await api.adminDecideAllowanceRequest(reqId, {
            decision,
            notes: notes || `${decision} by organizer`,
            actor_id: "organizer-admin",
          });
          notify(`Request ${decision.toLowerCase()} for ${participantName}.`);
          loadAdminAllowances();
        } catch (err) {
          notify(err.message || `Failed to ${decision.toLowerCase()} request.`);
          btn.disabled = false;
          btn.textContent = decision;
        }
      });
    });
  } catch (err) {
    handleAdminError(target, err);
  }
}

async function loadAdminControls() {
  const target = app.querySelector("#admin-content");
  if (!target) return;
  try {
    const overview = await api.adminOverview();
    if (!target.isConnected) return;
    markAdminFresh(target);
    target.innerHTML = `
      <div style="display: grid; gap: 24px;">
        <article style="background: #fff; border: 1px solid #dce2e9; border-radius: 6px; padding: 24px;">
          <h2 style="font-size: 16px; margin: 0 0 8px; color: #17233b;">Official Tournament Freeze Status</h2>
          <p style="font-size: 13px; color: #5d6878; margin-bottom: 16px;">
            The official tournament freeze permanently locks all participant submission selections and marks the end of the submission window.
          </p>
          <div style="margin-bottom: 20px;">
            ${overview.official_frozen ? `
              <div style="padding: 16px; border: 1px solid #c3e6cb; background: #d4edda; border-radius: 6px; color: #155724;">
                <strong>Official submissions are FROZEN.</strong>
                <p style="margin: 6px 0 0; font-size: 12px;">Frozen at: ${overview.official_freeze_at ? esc(dateTime(overview.official_freeze_at)) : "Recorded"}</p>
                <p style="margin: 4px 0 0; font-size: 12px;">Participant models are locked for tournament pairings. No further submissions or official selections can be changed.</p>
              </div>
            ` : `
              <div style="padding: 16px; border: 1px solid #ffeeba; background: #fff3cd; border-radius: 6px; color: #856404; margin-bottom: 20px;">
                <strong>Official submissions are currently OPEN.</strong>
                <p style="margin: 6px 0 0; font-size: 12px;">Participants can continue submitting packages and selecting their official version.</p>
              </div>
              <div style="border: 1px solid #edc6c6; border-left: 4px solid #dc4d4d; background: #fff5f5; padding: 20px; border-radius: 6px;">
                <h3 style="color: #842d2d; font-size: 14px; margin: 0 0 8px;">IRREVERSIBLE ACTION: Execute Official Freeze</h3>
                <p style="color: #5d6878; font-size: 12px; line-height: 1.5; margin-bottom: 16px;">
                  Freezing the tournament cannot be undone. Once executed, participants will no longer be able to select or modify official submissions. Current selections count: <strong>${overview.official_selections_count}</strong>.
                </p>
                <div style="display: flex; flex-direction: column; gap: 12px; max-width: 400px;">
                  <label for="freeze-confirm-input" style="font-size: 12px; font-weight: 600; color: #35435e;">
                    Type <code>FREEZE</code> to enable the freeze control:
                  </label>
                  <input type="text" id="freeze-confirm-input" autocomplete="off" placeholder="FREEZE" style="padding: 8px 12px; border: 1px solid #c7d2e0; border-radius: 4px; font-size: 13px;" />
                  <button type="button" id="btn-execute-freeze" class="button" style="background: #dc4d4d; color: #fff; padding: 10px 16px; font-weight: 700; cursor: not-allowed; opacity: 0.5;" disabled>Execute Official Tournament Freeze</button>
                </div>
              </div>
            `}
          </div>
        </article>
      </div>
    `;

    if (!overview.official_frozen) {
      const input = target.querySelector("#freeze-confirm-input");
      const btn = target.querySelector("#btn-execute-freeze");
      input?.addEventListener("input", () => {
        const ok = input.value.trim() === "FREEZE";
        btn.disabled = !ok;
        btn.style.opacity = ok ? "1" : "0.5";
        btn.style.cursor = ok ? "pointer" : "not-allowed";
      });

      btn?.addEventListener("click", async () => {
        if (input.value.trim() !== "FREEZE") return;
        if (!confirm("CRITICAL WARNING: This action is permanent and cannot be undone. Freeze official submissions now?")) return;
        btn.disabled = true;
        btn.textContent = "Executing Freeze…";
        try {
          await api.adminFreeze({
            reason_code: "OFFICIAL_BUILD_WINDOW_CLOSED",
            actor_id: "organizer-lead",
          });
          notify("Tournament officially FROZEN.");
          loadAdminControls();
        } catch (err) {
          notify(err.message || "Failed to execute freeze.");
          btn.disabled = false;
          btn.textContent = "Execute Official Tournament Freeze";
        }
      });
    }
  } catch (err) {
    handleAdminError(target, err);
  }
}

async function loadAdminAudit() {
  const target = app.querySelector("#admin-content");
  if (!target) return;
  try {
    const list = await api.adminAuditLog();
    if (!target.isConnected) return;
    markAdminFresh(target);
    if (!list || !list.length) {
      target.innerHTML = `<div style="padding: 24px; background: #fff; border: 1px solid #dce2e9; border-radius: 6px;"><p>No audit log events recorded yet.</p></div>`;
      return;
    }
    target.innerHTML = `
      <div class="data-table-wrap" style="background: #fff; border: 1px solid #dce2e9; border-radius: 6px; padding: 12px;">
        <table class="data-table">
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Severity</th>
              <th>Event Type</th>
              <th>Actor</th>
              <th>Aggregate</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            ${list.map((e) => `
              <tr>
                <td>${esc(dateTime(e.recorded_at))}</td>
                <td><span class="status-badge ${e.severity === "CRITICAL" ? "danger" : e.severity === "WARN" ? "warning" : "neutral"}">${esc(e.severity)}</span></td>
                <td><strong>${esc(e.event_type)}</strong></td>
                <td>${esc(e.actor_kind)}: <code>${esc(e.actor_id)}</code></td>
                <td>${esc(e.aggregate_type)}: <code>${esc(String(e.aggregate_id).slice(0, 8))}</code></td>
                <td><small><code>${esc(JSON.stringify(e.metadata || {}))}</code></small></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
  } catch (err) {
    handleAdminError(target, err);
  }
}

function renderBuild(participant) {
  shell(participant, "build", "AI Agent Engineering Workbench", `
    <div class="notice-callout warning">
      <i>!</i>
      <div>
        <strong>Participant code execution is disabled</strong>
        <p>Uploads receive static package validation only. Submitted code will not run in matches until the isolated execution worker passes its security review. This portal is not a cloud compiler.</p>
      </div>
    </div>

    <section class="workbench-grid">
      <div class="workbench-main">
        <article class="card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">STANDARD PARTICIPANT API · INTELLIGENCE.PY</span>
              <h2>Participant Agent Contract</h2>
            </div>
            <button class="button button-secondary" type="button" id="btn-copy-intelligence">Copy intelligence.py</button>
          </div>
          <p class="body-copy">The standard submission format is a single Python file named <code>intelligence.py</code>. The server owns the chess rules, authoritative board state, clocks, and legal move validation. Your function receives the board FEN, legal moves, and remaining time, and must return a valid UCI move.</p>
          <div class="spec-grid">
            <div class="spec-tile"><span>File name</span><strong>intelligence.py</strong><small>Single Python file</small></div>
            <div class="spec-tile"><span>Entrypoint</span><strong>choose_move(...)</strong><small>Required function</small></div>
            <div class="spec-tile"><span>Move format</span><strong>UCI notation</strong><small>e.g. 'e2e4', 'g1f3'</small></div>
          </div>

          <div class="code-box">
            <div class="code-box-header"><span>intelligence.py</span></div>
            <pre><code id="intelligence-code-block">def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    """Choose and return a legal chess move in UCI notation.

    Args:
        board_fen: Current board position in Forsyth-Edwards Notation (FEN).
        legal_moves: List of all legal moves in UCI format (e.g. ['e2e4', 'g1f3']).
        time_left_ms: Milliseconds remaining on your clock.

    Returns:
        A legal move string selected from legal_moves (e.g. 'e2e4').
    """
    # Baseline strategy: play the first available legal move
    return legal_moves[0]
</code></pre>
          </div>
        </article>

        <article class="card" style="margin-top: 16px;">
          <div class="card-heading">
            <div>
              <span class="eyebrow">CONTAINER PROTOCOL · AIK-JSONL/1</span>
              <h2>Engine I/O & Runtime Protocol</h2>
            </div>
            <span class="status-badge">Container option</span>
          </div>
          <p class="body-copy">For multi-file engines packaged in a <code>.tar</code> archive, your engine communicates via line-delimited JSON over standard I/O (stdin/stdout). <em>(Single-file <code>intelligence.py</code> submissions do not require this; the server automatically injects the protocol adapter).</em></p>
          <div class="spec-grid">
            <div class="spec-tile"><span>Protocol</span><strong>AIK JSONL/1</strong><small>Contract label</small></div>
            <div class="spec-tile"><span>Runtime label</span><strong>Python 3.12</strong><small>Manifest validation</small></div>
            <div class="spec-tile"><span>Package limit</span><strong>10 MiB</strong><small>Uncompressed USTAR archive</small></div>
          </div>

          <div class="code-box">
            <div class="code-box-header">
              <span>Protocol Messages (Line-Delimited JSON)</span>
            </div>
            <pre><code>// 1. Startup handshake:
// In:  {"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": "1"}
// Out: {"protocol_version": "aik-jsonl/1", "type": "ready", "request_id": "1"}

// 2. Position updates:
// In:  {"protocol_version": "aik-jsonl/1", "type": "position", "request_id": "2", "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "uci_moves": []}
// Out: {"protocol_version": "aik-jsonl/1", "type": "ack", "request_id": "2"}

// 3. Move calculation request:
// In:  {"protocol_version": "aik-jsonl/1", "type": "go", "request_id": "3", "clocks_ms": {"white": 180000, "black": 180000}}
// Out: {"protocol_version": "aik-jsonl/1", "type": "bestmove", "request_id": "3", "move": "e2e4"}</code></pre>
          </div>
        </article>

        <article class="card" style="margin-top: 16px;">
          <div class="card-heading">
            <div>
              <span class="eyebrow">STARTER AGENT IMPLEMENTATION</span>
              <h2>Python Starter Code (engine/main.py)</h2>
            </div>
            <button class="button button-secondary" type="button" id="btn-copy-starter">Copy Starter Code</button>
          </div>
          <p class="body-copy">Illustrative starter code conforming to the <code>aik-jsonl/1</code> specification for local development and testing.</p>

          <div class="code-box">
            <div class="code-box-header"><span>engine/main.py</span></div>
            <pre><code id="starter-code-block">import json
import sys
import chess

def select_move(board: chess.Board) -> str:
    for move in board.legal_moves:
        if board.is_capture(move):
            return move.uci()
    return next(iter(board.legal_moves)).uci()

def main():
    board = chess.Board()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as exc:
            print(f"Invalid engine message: {exc}", file=sys.stderr)
            continue
        mtype = msg.get("type")
        req_id = msg.get("request_id", "")
        proto = msg.get("protocol_version", "aik-jsonl/1")

        if mtype == "hello":
            resp = {"protocol_version": proto, "type": "ready", "request_id": req_id}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif mtype == "position":
            fen = msg.get("fen")
            if fen:
                try:
                    board = chess.Board(fen)
                except ValueError:
                    board = chess.Board()
            for m in msg.get("uci_moves", []):
                try:
                    board.push_uci(m)
                except ValueError:
                    continue
            resp = {"protocol_version": proto, "type": "ack", "request_id": req_id}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif mtype == "go":
            best_uci = select_move(board)
            resp = {"protocol_version": proto, "type": "bestmove", "request_id": req_id, "move": best_uci}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()</code></pre>
          </div>
        </article>
      </div>

      <div class="workbench-aside">
        <article class="card">
          <div class="card-heading">
            <h2>Package Manifest</h2>
            <span>manifest.json</span>
          </div>
          <p class="body-copy">Every uploaded archive must include a root <code>manifest.json</code> declaring the entrypoint and protocol version.</p>

          <div class="code-box">
            <pre><code>{
  "contract_version": "1",
  "protocol_version": "aik-jsonl/1",
  "engine_name": "MyChessEngine",
  "engine_version": "1.0.0",
  "runtime": "python-3.12",
  "entrypoint": "engine/main.py"
}</code></pre>
          </div>

          <div class="card-heading" style="margin-top: 16px;">
            <h3>Archive Packaging</h3>
          </div>
          <p class="body-copy">Create an uncompressed USTAR <code>.tar</code> archive (max 10 MiB) containing your manifest and code:</p>
          <div class="code-box">
            <pre><code># Directory layout:
# agent/
#   ├── manifest.json
#   ├── README.md
#   └── engine/
#       └── main.py

# Package command:
tar -cf my_agent.tar manifest.json README.md engine/</code></pre>
          </div>

          <div style="margin-top: 20px;">
            <button class="button button-primary button-block" type="button" data-page="submit">Continue to Submit Package →</button>
          </div>
        </article>

      </div>
    </section>
  `);

  app.querySelector("#btn-copy-intelligence")?.addEventListener("click", () => {
    const code = app.querySelector("#intelligence-code-block")?.textContent;
    if (code) {
      navigator.clipboard.writeText(code).then(() => notify("intelligence.py code copied to clipboard!")).catch(() => notify("Failed to copy code."));
    }
  });

  app.querySelector("#btn-copy-starter")?.addEventListener("click", () => {
    const code = app.querySelector("#starter-code-block")?.textContent;
    if (code) {
      navigator.clipboard.writeText(code).then(() => notify("Starter code copied to clipboard!")).catch(() => notify("Failed to copy code."));
    }
  });

}

function renderSubmit(participant) {
  shell(participant, "submit", "Submit Your Model", `
    <section class="submit-layout">
      <article class="card upload-card">
        <div class="card-heading">
          <div>
            <span class="eyebrow">UPLOAD → VALIDATION → ELIGIBILITY</span>
            <h2>Submit Your Engine Package</h2>
          </div>
          <span class="card-mark blue">↑</span>
        </div>

        <div class="quota-meter-wrap" id="quota-banner">
          <div class="quota-meter-header">
            <div>
              <span class="eyebrow" style="font-size: 11px;">SUBMISSION QUOTA</span>
              <div id="quota-info" style="font-size: 14px; font-weight: 600; color: var(--ink);">Loading quota…</div>
            </div>
            <button class="button button-secondary" type="button" id="btn-request-quota">Request Allowance</button>
          </div>
          <div class="quota-meter-bar">
            <div id="quota-bar-fill" class="quota-meter-fill" style="width: 0%;"></div>
          </div>
          <small id="official-version" class="official-version" style="margin-top: 8px;">Checking official selection…</small>
        </div>

        <form id="allowance-form" class="allowance-form" hidden style="margin-bottom: 16px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-raised);">
          <div class="card-heading"><h3>Request Submission Quota Exception</h3></div>
          <p class="body-copy">Organizer authorization is required for submissions beyond the default limit of 10 uploads.</p>
          <div class="field" style="margin-top: 10px;">
            <label for="allowance-limit">Requested Total Submissions Limit</label>
            <input class="input" id="allowance-limit" type="number" min="11" step="1" required>
          </div>
          <div class="field">
            <label for="allowance-reason">Technical Justification</label>
            <textarea class="input" id="allowance-reason" rows="3" maxlength="500" placeholder="Explain the model optimization, bugfix, or reason for additional attempts..." required></textarea>
          </div>
          <div style="display: flex; gap: 10px; margin-top: 12px;">
            <button class="button button-primary" type="submit">Send Request to Organizer</button>
            <button class="button button-secondary" id="allowance-cancel" type="button">Cancel</button>
          </div>
        </form>

        <div id="allowance-requests" class="allowance-requests" aria-live="polite"></div>

        <p class="body-copy">Upload your <code>intelligence.py</code> Python agent or an uncompressed USTAR archive (<code>.tar</code>, max 10 MiB). Uploaded bytes are cryptographically hashed and validated server-side.</p>

        <form id="upload-form" class="upload-form">
          <label class="file-drop" for="archive-file">
            <span class="upload-symbol" aria-hidden="true">↑</span>
            <strong>Select or Drag & Drop intelligence.py or .tar Archive</strong>
            <span>Python script (intelligence.py) or uncompressed .tar · maximum 10 MiB</span>
            <input id="archive-file" type="file" accept=".py,.tar,text/x-python,application/x-tar,application/octet-stream" required>
            <span id="file-name" class="file-name">No file selected</span>
            <span id="file-size" class="file-size"></span>
          </label>
          <p id="upload-error" class="form-message" role="alert"></p>
          <button class="button button-primary" type="submit">Submit Package for Server Validation <span>→</span></button>
        </form>
        <div id="upload-result" aria-live="polite"></div>
      </article>
    </section>

    <section class="card history-card" style="margin-top: 20px;">
      <div class="card-heading">
        <div>
          <span class="eyebrow">PERSISTED ARTIFACT RECORDS</span>
          <h2>Submission Version History</h2>
        </div>
        <button class="link-button" type="button" id="refresh-submission-history">Refresh server records</button>
      </div>
      <div id="submission-history">Loading submissions…</div>
    </section>
  `);

  const dropZone = app.querySelector(".file-drop");
  const fileInput = app.querySelector("#archive-file");
  if (dropZone && fileInput) {
    bindArchiveDrop(dropZone, fileInput);
    fileInput.addEventListener("change", (event) => {
      const file = event.target.files[0];
      const nameEl = app.querySelector("#file-name");
      const sizeEl = app.querySelector("#file-size");
      if (nameEl) nameEl.textContent = file?.name || "No file selected";
      if (sizeEl) sizeEl.textContent = file ? `${(file.size / 1024 / 1024).toFixed(2)} MiB` : "";
    });
  }
  app.querySelector("#upload-form").addEventListener("submit", uploadSubmission);
  app.querySelector("#refresh-submission-history")?.addEventListener("click", loadSubmissionHistory);
  const allowanceForm = app.querySelector("#allowance-form");
  app.querySelector("#btn-request-quota")?.addEventListener("click", async () => {
    try {
      const quota = await api.submissionQuota(sessionToken);
      const limit = app.querySelector("#allowance-limit");
      limit.min = String(quota.submission_limit + 1);
      limit.value = String(quota.submission_limit + 5);
      allowanceForm.hidden = false;
      limit.focus();
    } catch (error) {
      if (!handleSessionError(error)) notify(error.message || "Quota details are unavailable.");
    }
  });
  app.querySelector("#allowance-cancel")?.addEventListener("click", () => { allowanceForm.hidden = true; });
  allowanceForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = allowanceForm.querySelector("[type=submit]");
    const requestedLimit = Number(app.querySelector("#allowance-limit").value);
    const reason = app.querySelector("#allowance-reason").value.trim();
    submitButton.disabled = true;
    try {
      await api.requestAllowanceIncrease(sessionToken, { requested_limit: requestedLimit, reason });
      allowanceForm.hidden = true;
      notify("Allowance request sent for organizer review.");
      await loadSubmissionHistory();
    } catch (error) {
      if (!handleSessionError(error)) notify(error.message || "Could not submit the allowance request.");
    } finally { if (submitButton.isConnected) submitButton.disabled = false; }
  });
  loadSubmissionHistory();
}

async function uploadSubmission(event) {
  event.preventDefault();
  const file = app.querySelector("#archive-file").files[0];
  const button = event.currentTarget.querySelector("button[type=submit]");
  const errorNode = app.querySelector("#upload-error");
  const resultNode = app.querySelector("#upload-result");
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) { errorNode.textContent = "The archive must be 10 MiB or smaller."; return; }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.dataset.busy = "true";
  button.textContent = "Uploading…";
  errorNode.textContent = "";
  resultNode.textContent = "";
  try {
    let contentType = "application/x-tar";
    if (file.name.endsWith(".py") || (file.type && file.type.includes("python"))) {
      contentType = "text/x-python";
    } else if (file.name.endsWith(".tar") || (file.type && file.type.includes("tar"))) {
      contentType = "application/x-tar";
    } else if (file.type) {
      contentType = file.type;
    }
    const created = await api.createSubmission(sessionToken);
    const uploaded = await api.uploadVersion(sessionToken, created.submission_id, await file.arrayBuffer(), contentType);
    const validation = uploaded.validation || {};
    resultNode.innerHTML = `<div class="upload-outcome"><strong>Server recorded version ${esc(uploaded.version.version)}</strong>${badge(uploaded.version.state)}<p>Validation outcome: ${esc(validation.outcome || "Not reported")} · ${esc(validation.validator_version || "validator version unavailable")}</p><small>Artifact SHA-256: <code>${esc(uploaded.version.artifact_sha256)}</code></small></div>`;
    await loadSubmissionHistory();
    notify("Submission received by the server");
  } catch (error) {
    if (handleSessionError(error)) return;
    if (error.status === 422 && error.payload) {
      const v = error.payload.validation || {};
      const reason = error.payload.detail || error.payload.rejection_reason || error.payload.error || error.message || "The server rejected this package.";
      resultNode.innerHTML = `<div class="upload-outcome rejected"><strong>Package rejected</strong>${badge(v.outcome || "Rejected")}<p>${esc(reason)}</p><small>The rejected result is retained in server history.</small></div>`;
      await loadSubmissionHistory();
    } else errorNode.textContent = error.message || "Upload failed. The server did not confirm receipt.";
  } finally {
    if (button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); delete button.dataset.busy; button.innerHTML = 'Submit Package for Server Validation <span>→</span>'; }
  }
}

async function loadSubmissionHistory() {
  const target = app.querySelector("#submission-history");
  if (!target) return;
  const refreshButton = app.querySelector("#refresh-submission-history");
  if (refreshButton) { refreshButton.disabled = true; refreshButton.textContent = "Refreshing…"; }
  const results = await Promise.allSettled([
    api.submissionQuota(sessionToken),
    api.submissions(sessionToken),
    api.eventStatus(sessionToken),
    api.myAllowanceRequests(sessionToken),
  ]);
  try {
    const [quotaResult, submissionsResult, eventResult, allowanceResult] = results;
    for (const result of results) {
      if (result.status === "rejected" && handleSessionError(result.reason)) return;
    }
    if (!target.isConnected) return;
    const quotaState = resolveQuotaSnapshot(quotaResult);
    const quotaInfo = app.querySelector("#quota-info");
    const quotaBar = app.querySelector("#quota-bar-fill");
    if (quotaState.state === "available") {
      const quota = quotaState.quota;
      if (quotaInfo) quotaInfo.innerHTML = `<strong>${quota.accepted_submissions_count} of ${quota.submission_limit} accepted submissions used</strong> (${quota.remaining_submissions} remaining)${quota.has_pending_request ? ' · <span class="status-badge warning" style="font-size: 10px;">Exception pending</span>' : ""}`;
      if (quotaBar) {
        quotaBar.parentElement.hidden = false;
        const pct = Math.min(100, Math.round((quota.accepted_submissions_count / quota.submission_limit) * 100));
        quotaBar.style.width = `${pct}%`;
        quotaBar.className = pct >= 100 ? "quota-meter-fill danger" : pct >= 80 ? "quota-meter-fill warning" : "quota-meter-fill";
      }
    } else {
      if (quotaInfo) quotaInfo.textContent = "Quota status unavailable. Refresh to retry.";
      if (quotaBar?.parentElement) quotaBar.parentElement.hidden = true;
    }

    const requestTarget = app.querySelector("#allowance-requests");
    if (requestTarget) {
      if (allowanceResult.status === "rejected") requestTarget.innerHTML = `<p class="form-message">Allowance request status unavailable.</p>`;
      else {
        const requests = allowanceResult.value.items || [];
        requestTarget.innerHTML = requests.length ? `<p class="allowance-heading">Allowance requests</p>${requests.map((request) => `<div class="allowance-request-row"><span>${badge(request.status)}</span><span>Requested limit ${esc(request.requested_limit)}</span><time>${esc(dateTime(request.created_at))}</time></div>`).join("")}` : "";
      }
    }
    const eventStatus = eventResult.status === "fulfilled" ? eventResult.value : null;
    const official = eventStatus?.official_submission;
    const officialTarget = app.querySelector("#official-version");
    if (officialTarget) {
      officialTarget.innerHTML = !eventStatus
        ? `<span style="color: var(--warning);">Official selection status unavailable.</span>`
        : official
          ? `Official entry: <strong>Version ${esc(official.version)}</strong> selected ${eventStatus.official_frozen_at ? `(Frozen ${dateTime(eventStatus.official_frozen_at)})` : ""}`
          : `<span style="color: var(--warning);">No official tournament version selected yet</span>`;
    }
    if (submissionsResult.status === "rejected") {
      target.innerHTML = `<div class="empty-table-state" role="alert">Submission history is unavailable. Refresh server records to retry.</div>`;
      return;
    }
    const submissions = submissionsResult.value;
    const versionResults = await Promise.allSettled((submissions.items || []).slice(0, 12).map((item) => api.submissionVersions(sessionToken, item.submission_id)));
    for (const result of versionResults) {
      if (result.status === "rejected" && handleSessionError(result.reason)) return;
    }
    const versions = versionResults.flatMap((result) => result.status === "fulfilled" ? (result.value.items || []) : []);
    const someVersionsUnavailable = versionResults.some((result) => result.status === "rejected");
    if (!target.isConnected) return;
    if (!versions.length) {
      target.innerHTML = `<div class="empty-table-state">${someVersionsUnavailable ? "Version history is unavailable. Refresh to retry." : submissions.items?.length ? "No version records returned for these submissions." : "No submissions recorded yet. Use the upload form above to submit your first package."}</div>`;
      return;
    }
    target.innerHTML = `
      ${someVersionsUnavailable || !eventStatus ? `<p class="form-message">${someVersionsUnavailable ? "Some version records could not be loaded." : "Official selection is unavailable; submission actions are read-only."}</p>` : ""}
      <div class="data-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Version</th>
              <th>Validation</th>
              <th>Submitted</th>
              <th>Artifact SHA-256</th>
              <th>Official</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${versions.slice(0, 30).map((version) => {
              const isOfficial = official && String(official.version) === String(version.version);
              const canSelect = !isOfficial && !eventStatus.official_frozen_at && version.state === "ELIGIBLE";
              return `
                <tr>
                  <td><strong>v${esc(version.version)}</strong></td>
                  <td>${badge(version.state)}</td>
                  <td>${esc(dateTime(version.submitted_at))}</td>
                  <td><code>${esc(version.artifact_sha256?.slice(0, 12) || "Unavailable")}${version.artifact_sha256 ? "…" : ""}</code></td>
                  <td>
                    ${isOfficial
                      ? '<span class="status-badge success">OFFICIAL</span>'
                      : canSelect && eventStatus
                        ? `<button class="button button-secondary" style="padding: 3px 8px; min-height: 26px; font-size: 11px;" type="button" data-select-ver="${esc(version.version)}" data-sub-id="${esc(version.submission_id)}">Set Official</button>`
                        : '<span style="color: var(--muted); font-size: 12px;">—</span>'}
                  </td>
                  <td><button class="table-action" type="button" data-validation="${esc(version.submission_id)}" data-version="${esc(version.version)}">Inspect</button></td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `;
    target.querySelectorAll("[data-validation]").forEach((button) => button.addEventListener("click", () => renderSubmissionDetails(activeParticipant, button.dataset.validation, button.dataset.version)));
    target.querySelectorAll("[data-select-ver]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        btn.disabled = true;
        btn.textContent = "Updating…";
        await api.selectOfficialSubmission(sessionToken, {
          submission_id: btn.dataset.subId,
          version: Number(btn.dataset.selectVer),
        });
        notify(`Version ${btn.dataset.selectVer} set as official tournament entry!`);
        await loadSubmissionHistory();
      } catch (err) {
        if (!handleSessionError(err)) notify(err.message || "Failed to update official selection.");
        if (btn.isConnected) { btn.disabled = false; btn.textContent = "Set Official"; }
      }
    }));
  } catch (error) {
    if (!handleSessionError(error) && target.isConnected) target.textContent = "Submission history is unavailable. Refresh server records to retry.";
  } finally {
    if (refreshButton?.isConnected) { refreshButton.disabled = false; refreshButton.textContent = "Refresh server records"; }
  }
}

async function renderSubmissionDetails(participant, submissionId, version) {
  shell(participant, "submit", "Submission Details", `<section class="card submission-detail-card"><div class="card-heading"><div><span class="eyebrow">SERVER RECORDED ARTIFACT</span><h2>Submission version ${esc(version)}</h2></div><button class="button button-secondary" id="submission-back" type="button">← Back to History</button></div><div id="submission-detail-content" class="loading-copy">Loading exact version and validation evidence…</div></section>`);
  app.querySelector("#submission-back").addEventListener("click", () => renderSubmit(participant));
  try {
    const [versions, validation] = await Promise.all([api.submissionVersions(sessionToken, submissionId), api.validation(sessionToken, submissionId, version)]);
    const item = (versions.items || []).find((entry) => String(entry.version) === String(version));
    const target = app.querySelector("#submission-detail-content");
    if (!target || !item) return;
    const attempts = validation.attempts || [];
    const manifest = item.manifest || {};
    target.innerHTML = `<div class="submission-detail-summary"><div><span class="eyebrow">VALIDATION STATUS</span>${badge(item.state)}</div><div><span class="eyebrow">SUBMITTED</span><strong>${esc(dateTime(item.submitted_at))}</strong></div><div><span class="eyebrow">ARTIFACT SHA-256</span><code>${esc(item.artifact_sha256)}</code></div></div><section class="validation-evidence"><h3>Validation evidence</h3>${attempts.length ? `<ol class="validation-attempts">${attempts.map((attempt) => `<li><div>${badge(attempt.outcome)}<strong>${esc(attempt.validator_version)}</strong></div><time>${esc(dateTime(attempt.completed_at || attempt.started_at))}</time>${attempt.report ? `<pre>${esc(JSON.stringify(attempt.report, null, 2))}</pre>` : `<p>Detailed report not exposed by the server.</p>`}</li>`).join("")}</ol>` : `<p class="muted-copy">No validation attempts are recorded for this version.</p>`}</section><section class="manifest-summary"><h3>Manifest</h3><dl class="profile-facts"><div><dt>Engine</dt><dd>${esc(manifest.engine_name || "Not present in response")}</dd></div><div><dt>Engine version</dt><dd>${esc(manifest.engine_version || "Not present in response")}</dd></div><div><dt>Protocol</dt><dd>${esc(manifest.protocol_version || "Not present in response")}</dd></div><div><dt>Runtime</dt><dd>${esc(manifest.runtime || "Not present in response")}</dd></div></dl></section>`;
  } catch (error) { if (!handleSessionError(error)) app.querySelector("#submission-detail-content").textContent = "Submission details are unavailable."; }
}

async function requestDevelopmentMatch() {
  if (!PARTICIPANT_CODE_EXECUTION_ENABLED) {
    notify("Participant-code execution is disabled pending sandbox verification.");
    return;
  }
  try {
    const status = await api.eventStatus(sessionToken);
    const latest = status.latest_submission;
    if (!latest || latest.state !== "ELIGIBLE") return notify("An eligible submission version is required.");
    const idempotencyKey = crypto.randomUUID();
    await api.requestDevelopmentMatch(sessionToken, { submission_id: latest.submission_id, version: latest.version }, idempotencyKey);
    notify("Practice match request recorded on server!");
    renderMatches(activeParticipant);
  } catch (error) {
    if (handleSessionError(error)) return;
    notify(error.status === 409 ? "A practice match is already active or this request conflicts with server state." : error.message || "Could not request a practice match.");
  }
}

async function openTestMatchModal(participant) {
  if (!PARTICIPANT_CODE_EXECUTION_ENABLED) {
    notify("Participant-code execution is disabled pending sandbox verification. No match request was sent.");
    return;
  }
  document.getElementById("test-match-modal-overlay")?.remove();

  let submissionsList = [];
  try {
    const res = await api.submissions(sessionToken);
    submissionsList = res.items || [];
  } catch (err) {
    if (handleSessionError(err)) return;
    notify("Could not load submissions. Please check your connection.");
    return;
  }

  const validVersions = [];
  for (const s of submissionsList) {
    if (s.latest_version && ["APPROVED", "ELIGIBLE"].includes(s.latest_version.state)) {
      validVersions.push({
        submission_id: s.id || s.submission_id,
        version: s.latest_version.version,
        state: s.latest_version.state,
        digest: s.latest_version.artifact_sha256 || "",
      });
    }
  }

  if (validVersions.length === 0) {
    notify("A validated submission (APPROVED or ELIGIBLE) is required to play a test match. Please upload your intelligence.py first.");
    return;
  }

  const modalHtml = `
    <div class="test-match-modal-overlay" id="test-match-modal-overlay">
      <div class="test-match-modal-card" role="dialog" aria-modal="true" aria-labelledby="test-match-modal-title">
        <div class="test-match-modal-header">
          <div>
            <span class="eyebrow">PRACTICE MATCH</span>
            <h2 id="test-match-modal-title">Play Test Match vs House AI</h2>
          </div>
          <button class="test-match-close-btn" id="test-match-modal-close" type="button" aria-label="Close">✕</button>
        </div>

        <div class="test-match-disclaimer">
          <div class="disclaimer-icon">i</div>
          <div class="disclaimer-text">
            <strong>Server-Authoritative Test Environment</strong>
            <p>Your intelligence.py will run against the House AI only when participant-code execution is enabled by verified release gates. <em>Test matches do NOT affect official Swiss tournament standings or qualification rankings.</em></p>
          </div>
        </div>

        <form id="test-match-form" class="test-match-form">
          <div class="form-group">
            <label for="test-match-submission">Submission Version</label>
            <select id="test-match-submission" class="select-input" required>
              ${validVersions.map((v, i) => `
                <option value="${esc(v.submission_id)}|${esc(v.version)}" ${i === 0 ? "selected" : ""}>
                  v${esc(v.version)} · ${esc(v.state)} (${esc(v.digest ? v.digest.slice(0, 8) + '…' : 'Valid artifact')})
                </option>
              `).join("")}
            </select>
            <small class="form-help">Choose your validated submission artifact to play.</small>
          </div>

          <div class="form-group">
            <label for="test-match-opponent">House AI Opponent</label>
            <select id="test-match-opponent" class="select-input">
              <option value="AIK-HOUSE-001" selected>AIK-HOUSE-001 — Unified House AI (Opening Book + Minimax Search + PST Evaluator)</option>
              <option value="house-ai-maximum-difficulty">House AI — Maximum Difficulty (Minimax Depth 4, PST, MVV-LVA)</option>
              <option value="house-ai-benchmark-baseline">House AI — Benchmark Baseline (First Legal Move)</option>
            </select>
            <small class="form-help">AIK-HOUSE-001 combines opening book retrieval, phase-aware positional evaluation, and iterative alpha-beta search.</small>
          </div>

          <div class="form-group">
            <label>Play As (Color)</label>
            <div class="side-selection-group">
              <label class="side-radio-label">
                <input type="radio" name="test-match-side" value="WHITE" checked>
                <span class="side-card">
                  <span class="side-color-indicator">♔</span>
                  <span class="side-card-details">
                    <strong>White</strong>
                    <small>Plays first move</small>
                  </span>
                </span>
              </label>
              <label class="side-radio-label">
                <input type="radio" name="test-match-side" value="BLACK">
                <span class="side-card">
                  <span class="side-color-indicator">♚</span>
                  <span class="side-card-details">
                    <strong>Black</strong>
                    <small>House AI plays first</small>
                  </span>
                </span>
              </label>
            </div>
          </div>

          <div id="test-match-error" class="notice-callout error" style="display:none; margin-top: 12px;"></div>

          <div class="test-match-modal-actions">
            <button type="button" class="button button-secondary" id="test-match-cancel-btn">Cancel</button>
            <button type="submit" class="button button-primary" id="test-match-submit-btn">Start Test Match</button>
          </div>
        </form>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML("beforeend", modalHtml);

  const overlay = document.getElementById("test-match-modal-overlay");
  const form = document.getElementById("test-match-form");
  const closeBtn = document.getElementById("test-match-modal-close");
  const cancelBtn = document.getElementById("test-match-cancel-btn");
  const submitBtn = document.getElementById("test-match-submit-btn");
  const errBox = document.getElementById("test-match-error");

  const closeModal = () => overlay?.remove();

  closeBtn?.addEventListener("click", closeModal);
  cancelBtn?.addEventListener("click", closeModal);
  overlay?.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });

  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    errBox.style.display = "none";
    errBox.textContent = "";

    const subVal = document.getElementById("test-match-submission").value;
    const [submission_id, versionStr] = subVal.split("|");
    const version = Number(versionStr);
    const house_ai_id = document.getElementById("test-match-opponent").value;
    const side = form.querySelector('input[name="test-match-side"]:checked')?.value || "WHITE";

    submitBtn.disabled = true;
    submitBtn.textContent = "Launching Test Match…";

    try {
      const idempotencyKey = crypto.randomUUID();
      const match = await api.requestDevelopmentMatch(
        sessionToken,
        { submission_id, version, side, house_ai_id },
        idempotencyKey
      );
      closeModal();
      notify("Test match against House AI created!");
      renderMatchDetail(participant, match.match_id);
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Start Test Match";
      if (handleSessionError(err)) return;
      errBox.style.display = "flex";
      errBox.textContent = err.status === 409
        ? "A test match is already in progress. Please wait for it to finish."
        : err.message || "Failed to launch test match.";
    }
  });
}

function renderMatches(participant) {
  shell(participant, "matches", "Recorded Matches", `
    <div class="tab-strip">
      <button class="tab-btn active" type="button" data-match-tab="practice">Practice Matches (House AI)</button>
      <button class="tab-btn" type="button" data-match-tab="official">Official Tournament Matches</button>
    </div>

    <div id="match-tab-practice">
      <section class="match-banner">
        <div>
          <span class="phase-pill"><i></i> SERVER-RECORDED PRACTICE MATCHES</span>
          <h2>Practice Match Records</h2>
          <p class="body-copy">Server-recorded development match snapshots against House AI. New match requests remain disabled pending sandbox verification.</p>
        </div>
        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
          ${PARTICIPANT_CODE_EXECUTION_ENABLED ? `
            <button class="button button-primary" id="matches-play-test-match" type="button" title="Start a practice match">Play Test Match vs House AI</button>
          ` : `
            <span class="status-badge neutral">Sandbox Standby</span>
            <button class="button button-primary" id="matches-play-test-match" type="button" disabled style="display:none;" title="Participant-code execution is disabled pending sandbox verification.">Play Test Match vs House AI</button>
          `}
          <button class="button button-primary" id="matches-request" type="button" disabled style="display:none;" title="Participant-code execution is disabled pending sandbox verification.">Execution disabled</button>
          <button class="button button-secondary" id="matches-refresh" type="button">Refresh List ↻</button>
        </div>
      </section>

      <div class="notice-callout info">
        <i>i</i>
        <div>
          <strong>Execution status & data availability</strong>
          <p>Participant-code execution is disabled pending sandbox verification. No participant submission will be executed. Any displayed board position or move count comes from an authoritative server snapshot.</p>
        </div>
      </div>

      <section class="card matches-card" style="margin-top: 16px;">
        <div class="card-heading">
          <h2>Practice Matches</h2>
          <span class="muted-label">Server Match Records</span>
        </div>
        <div id="matches-list">Loading match records…</div>
      </section>
    </div>

    <div id="match-tab-official" hidden>
      <section class="card" style="margin-top: 16px;">
        <div class="card-heading">
          <div>
            <span class="eyebrow">OFFICIAL SWISS COMPETITION</span>
            <h2>Tournament Match Pairings</h2>
          </div>
          <button class="link-button" type="button" data-page="results">View Standings & Rounds →</button>
        </div>
        <div id="official-matches-list">Loading official tournament matches…</div>
      </section>
    </div>
  `);

  app.querySelectorAll("[data-match-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      app.querySelectorAll("[data-match-tab]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.matchTab;
      const practice = app.querySelector("#match-tab-practice");
      const official = app.querySelector("#match-tab-official");
      if (tab === "practice") {
        practice.hidden = false;
        official.hidden = true;
      } else {
        practice.hidden = true;
        official.hidden = false;
        loadOfficialMatches(participant);
      }
    });
  });

  app.querySelector("#matches-request").addEventListener("click", requestDevelopmentMatch);
  app.querySelector("#matches-play-test-match")?.addEventListener("click", () => openTestMatchModal(participant));
  app.querySelector("#matches-refresh").addEventListener("click", () => renderMatches(participant));
  loadMatches(participant);
}

async function loadMatches(participant) {
  const target = app.querySelector("#matches-list");
  try {
    const result = await api.developmentMatches(sessionToken);
    if (!target?.isConnected) return;
    const active = (result.items || []).some((match) => ["QUEUED", "STARTING", "RUNNING"].includes(match.state));
    const activeMatch = (result.items || []).find((match) => ["QUEUED", "STARTING", "RUNNING"].includes(match.state));
    const button = app.querySelector("#matches-request");
    if (button) {
      button.disabled = !PARTICIPANT_CODE_EXECUTION_ENABLED || active;
      button.textContent = active ? "Practice match active" : "Execution disabled";
      button.title = PARTICIPANT_CODE_EXECUTION_ENABLED ? "" : "Participant-code execution is disabled pending sandbox verification.";
    }
    const testMatchBtn = app.querySelector("#matches-play-test-match");
    if (testMatchBtn) {
      testMatchBtn.disabled = !PARTICIPANT_CODE_EXECUTION_ENABLED || active;
      testMatchBtn.textContent = !PARTICIPANT_CODE_EXECUTION_ENABLED
        ? "Execution disabled"
        : active
          ? (activeMatch?.state === "QUEUED" ? "Match Queued…" : "Match Active…")
          : "Play Test Match vs House AI";
      testMatchBtn.title = !PARTICIPANT_CODE_EXECUTION_ENABLED
        ? "Participant-code execution is disabled pending sandbox verification."
        : active
          ? "A practice match is currently active or queued"
          : "Play practice match vs House AI (AIK-HOUSE-001 Unified)";
    }
    const matches = result.items || [];
    if (!matches.length) {
      target.innerHTML = `
        <div class="empty-table-state">
          <strong>No practice matches recorded</strong>
          <p>You have not recorded any practice matches yet.</p>
          <p class="muted-copy" style="margin-top: 8px; font-size: 0.85rem;">Participant-code execution is disabled pending sandbox verification (<code>SANDBOX_ENABLED=False</code>, <code>WORKER_EXECUTION_MODE=mock</code>). New practice match requests are unavailable until the security release gates are verified.</p>
        </div>
      `;
      return;
    }
    target.innerHTML = `
      <div class="development-match-list">
        ${matches.map((match) => {
          let stateSubtext = "";
          if (match.state === "QUEUED") {
            stateSubtext = "Queued on server · Awaiting sandbox worker";
          } else if (match.state === "STARTING") {
            stateSubtext = "Starting · Initializing runtime";
          } else if (match.state === "RUNNING") {
            stateSubtext = `${match.current_ply || 0} plies played · Running`;
          } else if (match.state === "COMPLETED") {
            stateSubtext = `${match.current_ply || 0} plies · ${esc(match.result?.outcome || match.result_outcome || "Result recorded")}`;
          } else if (match.state === "ABORTED") {
            stateSubtext = `Aborted · ${esc(match.result?.failure || match.failure_reason || "Execution failed")}`;
          }
          return `
          <article class="development-match-row">
            <div class="match-icon">${navIcon("matches")}</div>
            <div class="match-subject">
              <strong>${esc(match.participant?.submission_id ? `Submission v${match.participant.submission_version} (${match.participant.color || 'White'})` : "Your AI")}</strong>
              <small>vs ${esc(match.house_ai?.house_ai_id === "house-ai-maximum-difficulty" ? "House AI (Maximum Difficulty)" : (match.house_ai?.house_ai_id || "House AI"))} · v${esc(match.house_ai?.version ?? "1")} (${match.house_ai?.color || 'Black'})</small>
            </div>
            <div class="match-time">
              <span>${esc(dateTime(match.completed_at || match.created_at))}</span>
              <small>${stateSubtext}</small>
            </div>
            ${badge(match.state)}
            <button class="button button-secondary" data-match="${esc(match.match_id)}" type="button">${match.state === "COMPLETED" ? "View Board & Result" : "Inspect Match"}</button>
          </article>
        `;}).join("")}
      </div>
    `;
    target.querySelectorAll("[data-match]").forEach((button) => button.addEventListener("click", () => renderMatchDetail(participant, button.dataset.match)));
  } catch (error) { if (!handleSessionError(error)) target.textContent = "Match service unavailable."; }
}

async function loadOfficialMatches(participant) {
  const target = app.querySelector("#official-matches-list");
  if (!target) return;
  try {
    const matches = await api.tournamentMatches();
    if (!target.isConnected) return;
    const myMatches = (matches || []).filter(
      (m) => m.white_participant_id === participant.participant_id || m.black_participant_id === participant.participant_id
    );
    if (!myMatches.length) {
      target.innerHTML = `<div class="empty-table-state"><p>No official tournament matches are scheduled for your participant ID yet. Official pairings will generate when Swiss rounds commence.</p></div>`;
      return;
    }
    target.innerHTML = `
      <div class="data-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Match</th>
              <th>Opponent</th>
              <th>Your Color</th>
              <th>Status</th>
              <th>Outcome</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${myMatches.map((m) => {
              const isWhite = m.white_participant_id === participant.participant_id;
              const opponent = isWhite ? m.black_name : m.white_name;
              return `
                <tr>
                  <td><code>${esc(String(m.match_id).slice(0, 8))}</code></td>
                  <td><strong>${esc(opponent || "TBD")}</strong></td>
                  <td><span class="player-color ${isWhite ? "white" : "black"}">${isWhite ? "White" : "Black"}</span></td>
                  <td>${badge(m.state)}</td>
                  <td>${esc(m.result_outcome || "Pending")}</td>
                  <td><button class="button button-secondary button-small" data-match="${esc(m.match_id)}" type="button">Inspect</button></td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `;
    target.querySelectorAll("[data-match]").forEach((button) => button.addEventListener("click", () => renderMatchDetail(participant, button.dataset.match)));
  } catch {
    if (target.isConnected) target.innerHTML = `<div class="empty-table-state"><p>Tournament match records are not yet available.</p></div>`;
  }
}

async function renderMatchDetail(participant, matchId) {
  if (matchDetailTimer) {
    clearTimeout(matchDetailTimer);
    matchDetailTimer = null;
  }
  shell(participant, "matches", "Match Details", `
    <section class="card match-detail-card">
      <div class="card-heading">
        <div>
          <span class="phase-pill"><i></i> MATCH INSPECTION</span>
          <h2>Match Details: <code>${esc(matchId.slice(0, 8))}</code></h2>
        </div>
        <div style="display: flex; gap: 8px;">
          <button class="button button-secondary" id="match-refresh" type="button">Refresh State ↻</button>
          <button class="button button-secondary" id="match-back" type="button">← Back to Arena</button>
        </div>
      </div>
      <div id="match-detail-content" class="loading-copy">Loading authoritative match state…</div>
    </section>
  `);
  app.querySelector("#match-back").addEventListener("click", () => {
    if (matchDetailTimer) { clearTimeout(matchDetailTimer); matchDetailTimer = null; }
    renderMatches(participant);
  });
  app.querySelector("#match-refresh").addEventListener("click", () => updateMatchDetailContent(participant, matchId));
  await updateMatchDetailContent(participant, matchId);
}

async function updateMatchDetailContent(participant, matchId) {
  if (matchDetailTimer) {
    clearTimeout(matchDetailTimer);
    matchDetailTimer = null;
  }
  const targetContent = app.querySelector("#match-detail-content");
  if (!targetContent) return;
  try {
    let match = null;
    if (sessionToken) {
      try {
        match = await api.developmentMatch(sessionToken, matchId);
      } catch (devErr) {
        if (devErr.status !== 404) throw devErr;
      }
    }
    if (!match) {
      match = await api.tournamentMatch(matchId);
    }
    if (!app.querySelector("#match-detail-content")) return;
    const state = match.state;
    const isOfficial = match.purpose === "OFFICIAL" || (!match.house_ai && (match.white_name || match.black_name));
    let whitePlayerName = "White";
    let whitePlayerMeta = "Tournament Competitor";
    let blackPlayerName = "Black";
    let blackPlayerMeta = "Tournament Competitor";

    if (isOfficial) {
      whitePlayerName = match.white_name || "White";
      blackPlayerName = match.black_name || "Black";
    } else {
      const partSide = match.participant?.color || "WHITE";
      const partName = match.participant?.submission_id
        ? `Participant · v${match.participant.submission_version}`
        : "Participant AI";
      const partMeta = match.participant?.artifact_sha256
        ? match.participant.artifact_sha256.slice(0, 16) + "…"
        : "Artifact registered";

      const isMaxDiff = match.house_ai?.house_ai_id === "house-ai-maximum-difficulty";
      const isUnified = match.house_ai?.house_ai_id === "AIK-HOUSE-001";
      const houseName = isUnified
        ? "Unified House AI (AIK-HOUSE-001)"
        : isMaxDiff
        ? "House AI (Maximum Difficulty)"
        : (match.house_ai?.house_ai_id ? `${match.house_ai.house_ai_id} · v${match.house_ai.version ?? "1"}` : "House AI");
      const houseMeta = isUnified
        ? "Book + Minimax Search + PST Evaluator"
        : isMaxDiff ? "Iterative Minimax (Depth 4)" : "Baseline Opponent";

      if (partSide === "WHITE") {
        whitePlayerName = partName;
        whitePlayerMeta = partMeta;
        blackPlayerName = houseName;
        blackPlayerMeta = houseMeta;
      } else {
        whitePlayerName = houseName;
        whitePlayerMeta = houseMeta;
        blackPlayerName = partName;
        blackPlayerMeta = partMeta;
      }
    }

    const isTestMax = !isOfficial && match.house_ai?.house_ai_id === "house-ai-maximum-difficulty";
    const isTestUnified = !isOfficial && match.house_ai?.house_ai_id === "AIK-HOUSE-001";
    const badgeLabel = isOfficial
      ? '<span class="status-badge" style="background:#4a154b;color:#f3e8ff;">OFFICIAL TOURNAMENT MATCH</span>'
      : isTestUnified
        ? '<span class="status-badge" style="background:#1e3a5f;color:#93c5fd;">TEST MATCH · UNIFIED HOUSE AI (AIK-HOUSE-001)</span>'
        : isTestMax
        ? '<span class="status-badge" style="background:#1e3a5f;color:#93c5fd;">TEST MATCH · HOUSE AI (MAXIMUM DIFFICULTY)</span>'
        : '<span class="status-badge" style="background:#1e3a5f;color:#93c5fd;">PRACTICE MATCH (HOUSE AI / MOCK)</span>';

    const hasMoves = Array.isArray(match.moves) && match.moves.length > 0;
    const isQueued = state === "QUEUED";
    const resultOutcome = isQueued
      ? "QUEUED (Pending Worker Execution)"
      : (match.result?.outcome || match.result_outcome || state.replaceAll("_", " "));
    const resultReason = isQueued
      ? "Match recorded on authoritative server. Live execution requires an authorized worker daemon (SANDBOX_ENABLED=False, WORKER_EXECUTION_MODE=mock)."
      : (match.result?.reason || match.result_reason || "Game in progress");
    const resultFailure = match.result?.failure;

    app.querySelector("#match-detail-content").innerHTML = `
      <div class="match-detail-grid">
        <div class="match-overview">
          <div class="match-state-line">${badge(state)} ${badgeLabel} <span>${esc(dateTime(match.started_at || match.created_at))}</span>${state === "COMPLETED" ? `<span class="result-source">${resultSourceLabel(match.result_source)}</span>` : ""}</div>
          ${isQueued ? `
            <div class="notice-callout warning" style="margin-top: 12px; margin-bottom: 12px;">
              <i>!</i>
              <div>
                <strong>Execution Sandbox Pending Verification</strong>
                <p>This match is securely queued in the database. In the current staging environment, <code>SANDBOX_ENABLED=False</code> and <code>WORKER_EXECUTION_MODE=mock</code>. Submitted participant code will not run in the shared API process. To complete real games, an authorized, isolated worker runner must be provisioned and connected to the worker gateway.</p>
              </div>
            </div>
          ` : ""}
          <div class="player-pair">
            <div class="player-card">
              <span class="player-color white">W</span>
              <span><strong>${esc(whitePlayerName)}</strong><small>${esc(whitePlayerMeta)}</small></span>
            </div>
            <div class="player-card">
              <span class="player-color black">B</span>
              <span><strong>${esc(blackPlayerName)}</strong><small>${esc(blackPlayerMeta)}</small></span>
            </div>
          </div>
          <dl class="match-facts">
            <div><dt>Clock Setting</dt><dd>${esc((match.initial_clock_ms || 180000) / 60000)}+${esc((match.increment_ms || 0) / 1000)}s</dd></div>
            <div><dt>Move Deadline</dt><dd>${esc(match.move_timeout_ms || 5000)} ms</dd></div>
            <div><dt>Protocol</dt><dd>${esc(match.protocol_version || "aik-jsonl/1")}</dd></div>
            <div><dt>Current Plies</dt><dd>${esc(match.current_ply ?? 0)}</dd></div>
          </dl>
          <div class="result-panel">
            <span class="eyebrow">SERVER MATCH RESULT</span>
            <strong>${esc(resultOutcome)}</strong>
            <p>${esc(resultReason)}${resultFailure ? ` · ${esc(resultFailure)}` : ""}</p>
          </div>
        </div>
        <div class="board-panel">
          <div class="card-heading"><h3>Board Position</h3><small>${esc(match.current_ply ?? 0)} plies recorded</small></div>
          <div id="detail-board-stage">
            ${renderChessboard(match.current_fen)}
          </div>
          <div class="clock-row">
            <span>White <strong>${formatClock(match.white_remaining_ms)}</strong></span>
            <span>Black <strong>${formatClock(match.black_remaining_ms)}</strong></span>
          </div>
          ${hasMoves ? `
            <div class="interactive-replay-section">
              <div class="replay-controls-bar">
                <div class="replay-buttons">
                  <button class="button button-secondary button-small" id="replay-start" type="button" title="Start position">|◀</button>
                  <button class="button button-secondary button-small" id="replay-prev" type="button" title="Previous ply">◀</button>
                  <span id="replay-step-info" class="replay-step-label">Ply ${match.moves.length} of ${match.moves.length}</span>
                  <button class="button button-secondary button-small" id="replay-next" type="button" title="Next ply">▶</button>
                  <button class="button button-secondary button-small" id="replay-end" type="button" title="Final position">▶|</button>
                </div>
                <span class="replay-hint">Select any move to inspect board state</span>
              </div>
              <div class="move-transcript-scroll">
                <table class="data-table move-transcript-table">
                  <thead><tr><th>Ply</th><th>Turn</th><th>Move</th><th>Time</th></tr></thead>
                  <tbody>
                    ${match.moves.map((m, idx) => `
                      <tr class="move-transcript-row${idx === match.moves.length - 1 ? " active-ply" : ""}" data-ply-idx="${idx}">
                        <td>#${m.ply}</td>
                        <td>${m.ply % 2 === 1 ? "White" : "Black"}</td>
                        <td><strong><code>${esc(m.move_uci)}</code></strong></td>
                        <td><small>${m.elapsed_ms != null ? `${m.elapsed_ms} ms` : "—"}</small></td>
                      </tr>
                    `).join("")}
                  </tbody>
                </table>
              </div>
            </div>
            <p class="match-history-note" style="display:none">Move history is not available from the current API.</p>
          ` : `
            <p class="match-history-note">Move history is not available from the current API.</p>
          `}
        </div>
      </div>
    `;

    if (hasMoves) {
      let currentPlyIdx = match.moves.length - 1;
      const boardStage = app.querySelector("#detail-board-stage");
      const updatePly = (idx) => {
        currentPlyIdx = Math.max(-1, Math.min(match.moves.length - 1, idx));
        let fen = match.initial_fen || INITIAL_POSITION_FEN;
        let from = null;
        let to = null;
        if (currentPlyIdx >= 0) {
          const m = match.moves[currentPlyIdx];
          fen = m.position_after_fen || fen;
          from = m.from_square;
          to = m.to_square;
        }
        if (boardStage) updateCinematicBoard(boardStage, fen, { from, to });
        const stepLabel = app.querySelector("#replay-step-info");
        if (stepLabel) {
          stepLabel.textContent = currentPlyIdx === -1 ? `Start (0 of ${match.moves.length})` : `Ply ${currentPlyIdx + 1} of ${match.moves.length}`;
        }
        app.querySelectorAll(".move-transcript-row").forEach((row) => {
          const isActive = Number(row.dataset.plyIdx) === currentPlyIdx;
          row.classList.toggle("active-ply", isActive);
          if (isActive) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
        });
      };

      app.querySelector("#replay-start")?.addEventListener("click", () => updatePly(-1));
      app.querySelector("#replay-prev")?.addEventListener("click", () => updatePly(currentPlyIdx - 1));
      app.querySelector("#replay-next")?.addEventListener("click", () => updatePly(currentPlyIdx + 1));
      app.querySelector("#replay-end")?.addEventListener("click", () => updatePly(match.moves.length - 1));
      app.querySelectorAll(".move-transcript-row").forEach((row) => {
        row.addEventListener("click", () => updatePly(Number(row.dataset.plyIdx)));
      });
    }

    if (["STARTING", "RUNNING"].includes(state)) {
      matchDetailTimer = setTimeout(() => {
        if (app.querySelector("#match-detail-content")) {
          updateMatchDetailContent(participant, matchId);
        }
      }, 2000);
    } else if (state === "QUEUED") {
      matchDetailTimer = setTimeout(() => {
        if (app.querySelector("#match-detail-content")) {
          updateMatchDetailContent(participant, matchId);
        }
      }, 5000);
    }
  } catch (error) {
    if (!handleSessionError(error)) {
      app.querySelector("#match-detail-content").textContent = error.status === 404 ? "Match not found." : "Match state unavailable.";
    }
  }
}

function formatClock(milliseconds = 0) {
  if (!Number.isFinite(milliseconds)) return "—";
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function renderChessboard(fen) {
  return renderCinematicBoard(fen);
}

function renderLeaderboard(participant) {
  renderResults(participant);
}

function renderResults(participant) {
  shell(participant, "leaderboard", "Official Tournament Standings", `
    <div class="tab-strip">
      <button class="tab-btn active" type="button" data-res-tab="leaderboard">Official Leaderboard</button>
      <button class="tab-btn" type="button" data-res-tab="bracket">Swiss Rounds & Playoff Bracket</button>
    </div>

    <div id="res-tab-leaderboard">
      <section class="card leaderboard-card">
        <div class="card-heading">
          <div>
            <span class="eyebrow">OFFICIAL SWISS TOURNAMENT STANDINGS</span>
            <h2>Competition Standings</h2>
          </div>
          <div id="leaderboard-meta" class="muted-label"></div>
        </div>
        <div id="leaderboard-content" class="loading-copy">Loading official standings…</div>
      </section>
    </div>

    <div id="res-tab-bracket" hidden>
      <section class="tournament-view-grid">
        <article class="card bracket-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">OFFICIAL · SWISS SYSTEM</span>
              <h2>Swiss Qualifying Rounds</h2>
            </div>
            <span id="swiss-count" class="muted-label"></span>
          </div>
          <div id="swiss-content" class="loading-copy">Loading Swiss rounds…</div>
        </article>
        <article class="card playoff-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">OFFICIAL · SUDDEN-DEATH PLAYOFF</span>
              <h2>Top 8 Championship Bracket</h2>
            </div>
            <span id="playoff-count" class="muted-label"></span>
          </div>
          <div id="playoff-content" class="loading-copy">Loading playoff rounds…</div>
        </article>
      </section>
    </div>
  `);

  app.querySelectorAll("[data-res-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      app.querySelectorAll("[data-res-tab]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.resTab;
      const lb = app.querySelector("#res-tab-leaderboard");
      const br = app.querySelector("#res-tab-bracket");
      if (lb) lb.hidden = tab !== "leaderboard";
      if (br) br.hidden = tab !== "bracket";
      if (tab === "bracket") loadTournamentBracket(participant);
    });
  });

  loadTournamentLeaderboard(participant);
}

function renderTournamentBracketView(participant) {
  renderResults(participant);
  const bracketTab = app.querySelector('[data-res-tab="bracket"]');
  if (bracketTab) bracketTab.click();
}

function renderTournamentLeaderboardView(participant) {
  renderResults(participant);
}

function renderTournamentRound(round) {
  const pairings = Array.isArray(round.pairings) ? round.pairings : [];
  return `
    <section class="tournament-round-block">
      <div class="round-heading">
        <h3>${esc(round.round_name || `Round ${round.round_number}`)}</h3>
        ${badge(round.status || "Unavailable")}
      </div>
      ${pairings.length ? `
        <div class="data-table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>Pairing</th>
                <th>Participant 1</th>
                <th>Participant 2</th>
                <th>Score</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              ${pairings.map((pairing) => `
                <tr>
                  <td>${pairing.is_bye ? "Bye" : `#${Number(pairing.pairing_index) + 1}`}</td>
                  <td><strong>${esc(pairing.participant1_name || "Unassigned")}</strong></td>
                  <td><strong>${esc(pairing.is_bye ? "Bye" : pairing.participant2_name || "Pending")}</strong></td>
                  <td>${esc(pairing.p1_points ?? 0)} – ${esc(pairing.p2_points ?? 0)}</td>
                  <td>${badge(pairing.status || "Pending")}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      ` : `<p class="empty-table-state">No pairings returned for this round.</p>`}
    </section>
  `;
}

async function loadTournamentBracket(participant) {
  const swissTarget = app.querySelector("#swiss-content");
  const playoffTarget = app.querySelector("#playoff-content");
  if (!swissTarget || !playoffTarget) return;
  try {
    const [swissRounds, playoffRounds] = await Promise.all([
      api.tournamentRounds("SWISS"),
      api.tournamentRounds("PLAYOFF"),
    ]);
    if (!swissTarget.isConnected) return;
    const swiss = Array.isArray(swissRounds) ? swissRounds : [];
    const playoffs = Array.isArray(playoffRounds) ? playoffRounds : [];
    const sc = app.querySelector("#swiss-count");
    const pc = app.querySelector("#playoff-count");
    if (sc) sc.textContent = `${swiss.length} rounds`;
    if (pc) pc.textContent = `${playoffs.length} rounds`;
    swissTarget.innerHTML = swiss.length
      ? swiss.map(renderTournamentRound).join("")
      : `<div class="empty-table-state"><p>The server returned no Swiss rounds. This endpoint currently cannot distinguish an empty tournament from a round-read failure; refresh or ask an organizer to verify.</p></div>`;
    playoffTarget.innerHTML = playoffs.length
      ? playoffs.map(renderTournamentRound).join("")
      : `<div class="empty-table-state"><p>The server returned no playoff rounds. This endpoint currently cannot distinguish an empty tournament from a round-read failure; refresh or ask an organizer to verify.</p></div>`;
  } catch (error) {
    if (!handleSessionError(error)) {
      swissTarget.innerHTML = `<div class="empty-table-state"><p>Tournament rounds data is currently unavailable.</p></div>`;
      playoffTarget.innerHTML = `<div class="empty-table-state"><p>Playoff data is currently unavailable.</p></div>`;
    }
  }
}

async function loadTournamentLeaderboard(participant) {
  const target = app.querySelector("#leaderboard-content");
  const meta = app.querySelector("#leaderboard-meta");
  if (!target) return;
  try {
    const data = await api.tournamentLeaderboard();
    if (!target.isConnected) return;
    const rawStandings = Array.isArray(data.standings) ? data.standings : [];
    const standings = rawStandings.filter((s) => !s.is_house_ai && s.purpose !== "DEVELOPMENT");
    const completedMatches = Number(data.total_matches) || 0;
    const hasActiveResults = completedMatches > 0 && standings.some((s) => (s.matches_played || 0) > 0 || (s.points || 0) > 0);

    if (meta) {
      meta.textContent = `${standings.length} participants registered · ${hasActiveResults ? `${completedMatches} official matches completed` : "Round 1 Pending (0 official matches completed)"}`;
    }
    if (!standings.length) {
      target.innerHTML = `
        <div class="empty-table-state">
          <p>No tournament standings recorded yet. Standings will compute dynamically as Swiss rounds are played.</p>
        </div>
      `;
      return;
    }
    const myId = participant?.participant_id;
    target.innerHTML = `
      ${!hasActiveResults ? `
        <div class="notice-callout info leaderboard-pending-card" style="margin-bottom: 16px;">
          <div>
            <strong>Official Tournament Standings Pending · Round 1 Scheduled</strong>
            <p>Official Swiss rounds have not commenced. All ${standings.length} participants are in pre-tournament registered status. Authoritative rankings, points, and tie-breaks will compute live as matches are concluded.</p>
            <div class="scoring-rules-legend">
              <strong>Official Scoring:</strong> 1.0 pt Win · 0.5 pt Draw · 0.0 pt Loss &nbsp;|&nbsp; <strong>Tie-Breaks:</strong> Head-to-Head (H2H) → Opponent Strength (Buchholz) → Sonneborn-Berger
            </div>
          </div>
        </div>
      ` : `
        <div class="scoring-rules-legend" style="margin-bottom: 12px;">
          <strong>Official Scoring:</strong> 1.0 pt Win · 0.5 pt Draw · 0.0 pt Loss &nbsp;|&nbsp; <strong>Tie-Breaks:</strong> Head-to-Head (H2H) → Opponent Strength (Buchholz) → Sonneborn-Berger
        </div>
      `}
      <div class="data-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Rank</th>
              <th>Participant / AI</th>
              <th>Status</th>
              <th>Points</th>
              <th>W</th>
              <th>D</th>
              <th>L</th>
              <th>Tie-break (H2H / Opp)</th>
            </tr>
          </thead>
          <tbody>
            ${standings.map((s, idx) => {
              const isMe = s.participant_id === myId;
              const tb = s.opponent_strength != null ? `Opp: ${Number(s.opponent_strength).toFixed(2)}` : (s.sonneborn_berger ? `S-B: ${s.sonneborn_berger.toFixed(2)}` : "—");
              const rankDisplay = hasActiveResults ? `<strong>#${s.rank}</strong>` : `<span style="color:var(--muted);">—</span>`;
              const row = `
                <tr class="${s.status === "CHAMPION" ? "champion-row" : ""} ${isMe ? "my-standing-row" : ""}">
                  <td>${rankDisplay}</td>
                  <td><strong>${esc(s.name)}</strong>${s.team_name ? ` <small>(${esc(s.team_name)})</small>` : ""}${isMe ? ' <span class="status-badge success" style="font-size: 10px;">YOU</span>' : ''}</td>
                  <td>${badge(s.status)}</td>
                  <td><strong>${s.points != null ? s.points : "0.0"}</strong></td>
                  <td>${s.wins ?? 0}</td>
                  <td>${s.draws ?? 0}</td>
                  <td>${s.losses ?? 0}</td>
                  <td><small>${esc(tb)}</small></td>
                </tr>
              `;
              if (hasActiveResults && idx === 7 && standings.length > 8) {
                return row + `<tr class="top8-cutoff-row"><td colspan="8">——— TOP 8 QUALIFICATION CUTOFF (ADVANCE TO PLAYOFFS) ———</td></tr>`;
              }
              return row;
            }).join("")}
          </tbody>
        </table>
      </div>
    `;
  } catch (error) {
    if (!handleSessionError(error)) target.innerHTML = `<div class="empty-table-state"><p>Official standings are not yet seeded for this event.</p></div>`;
  }
}

async function loadResults(participant) {
  try {
    const response = await api.developmentMatches(sessionToken);
    const completed = (response.items || []).filter((match) => match.state === "COMPLETED" || match.state === "ABORTED");
    const target = app.querySelector("#results-list");
    if (!target) return;
    if (!completed.length) { target.innerHTML = `<div class="empty-table-state">No completed practice results recorded yet.</div>`; return; }
    target.innerHTML = `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Match</th><th>House AI</th><th>State</th><th>Result</th><th>Completed</th><th></th></tr></thead><tbody>${completed.map((match) => `<tr><td><code>${esc(match.match_id.slice(0, 8))}</code></td><td>${esc(match.house_ai?.house_ai_id || "—")} · v${esc(match.house_ai?.version ?? "1")}</td><td>${badge(match.state)}</td><td>${esc(match.result?.outcome || "No result")}${match.result?.reason ? ` · ${esc(match.result.reason)}` : ""}</td><td>${esc(dateTime(match.completed_at))}</td><td><button class="table-action" data-match="${esc(match.match_id)}" type="button">Details</button></td></tr>`).join("")}</tbody></table></div>`;
    target.querySelectorAll("[data-match]").forEach((button) => button.addEventListener("click", () => renderMatchDetail(participant, button.dataset.match)));
  } catch (error) { if (!handleSessionError(error)) app.querySelector("#results-list").textContent = "Result service unavailable."; }
}

function renderProfile(participant) {
  shell(participant, "profile", "Profile", `
    <div class="profile-grid">
      <section class="profile-card-section card">
        <div class="card-heading"><div><span class="eyebrow">PARTICIPANT IDENTITY</span><h2>Identity</h2></div>${badge(participant.status || "ACTIVE")}</div>
        <dl class="profile-facts">
          <div class="full-width"><dt>Participant ID</dt><dd><div class="credential-display-box"><code>${esc(participant.participant_id)}</code><button class="button button-secondary" type="button" id="btn-copy-pid">Copy</button></div></dd></div>
          <div><dt>Model / entry</dt><dd>${esc(participant.name || "—")}</dd></div>
          <div><dt>Team</dt><dd>${esc(participant.team_name || "—")}</dd></div>
        </dl>
      </section>
      <section class="profile-card-section card">
        <div class="card-heading"><div><span class="eyebrow">SESSION SECURITY</span><h2>Session</h2></div><span class="status-badge success">Active</span></div>
        <p class="body-copy">The access key is never shown here. The session token stays in memory and ends when you sign out or reload this page.</p>
        <div class="credential-display-box"><span>ACCESS KEY</span><code aria-label="Access key hidden">••••••••••••••••</code></div>
        <button class="button button-secondary button-block" type="button" data-signout>Sign out and revoke session</button>
      </section>
      <section class="profile-card-section card">
        <div class="card-heading"><div><span class="eyebrow">QUOTA & OFFICIAL ENTRY</span><h2>Competition access</h2></div></div>
        <div id="profile-quota-info" class="loading-copy">Loading quota and official-entry status…</div>
        <button class="button button-primary" type="button" data-page="submit">Manage submissions</button>
      </section>
    </div>
  `);
  app.querySelector("#btn-copy-pid")?.addEventListener("click", () => {
    navigator.clipboard.writeText(participant.participant_id).then(() => notify("Participant ID copied to clipboard.")).catch(() => notify("Clipboard unavailable."));
  });
  loadProfileQuota();
}

async function loadProfileQuota() {
  const target = app.querySelector("#profile-quota-info");
  if (!target) return;
  try {
    const [quota, eventStatus] = await Promise.all([
      api.submissionQuota(sessionToken),
      api.eventStatus(sessionToken),
    ]);
    if (!target.isConnected) return;
    const official = eventStatus.official_submission;
    target.innerHTML = `
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; font-size: 13px;">
        <div class="spec-tile">
          <span>Accepted Uploads</span>
          <strong>${quota.accepted_submissions_count} / ${quota.submission_limit}</strong>
          <small>${quota.remaining_submissions} remaining</small>
        </div>
        <div class="spec-tile">
          <span>Extra Allowance</span>
          <strong>${quota.approved_additional_allowance > 0 ? `+${quota.approved_additional_allowance}` : "None"}</strong>
          <small>${quota.has_pending_request ? "Exception pending" : "Standard limit"}</small>
        </div>
      </div>
      <div style="margin-top: 12px; font-size: 13px; color: var(--ink-soft);">
        <span>Official Entry: </span><strong>${official ? `Version ${esc(official.version)} (${eventStatus.official_frozen_at ? "Frozen" : "Eligible"})` : "Not selected"}</strong>
      </div>
    `;
  } catch {
    if (target.isConnected) target.textContent = "Quota details unavailable.";
  }
}

function handleSessionError(error) {
  if (error.status === 401 || error.status === 403) {
    sessionToken = null;
    landing("login", "Your session expired or was revoked. Sign in again.");
    return true;
  }
  return false;
}

async function loadProfile() {
  if (!sessionToken) return landing("login");
  try {
    const profile = await api.me(sessionToken);
    activeParticipant = profile;
    navigate(route === "login" || route === "dashboard" ? "overview" : route, profile);
  } catch (error) {
    if (!handleSessionError(error)) notify(error.message || "Could not refresh the profile");
  }
}

async function signOut() {
  const token = sessionToken;
  sessionToken = null;
  activeParticipant = null;
  landing("login");
  let revoked = !token;
  try { if (token) { await api.logout(token); revoked = true; } }
  catch { /* The local credential is cleared even when server revocation cannot be confirmed. */ }
  notify(revoked
    ? "Signed out. Server session revoked."
    : "Signed out on this device; server session revocation could not be confirmed.");
}

window.landing = landing;
window.renderRegistrationSuccess = renderRegistrationSuccess;
window.navigate = navigate;
window.getActiveParticipant = () => activeParticipant;
if (window.location.hash === "#admin") renderAdmin("matches");
else if (window.location.hash === "#arena") renderArena("live");
else if (window.location.hash === "#register") landing("register");
else landing("login");
