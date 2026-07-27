"""Harvesting a round's claims from the verifier's report.

A round's durable output is its claim list: ``VERIFIED`` ones become trusted state that nobody
re-derives later, and that is what keeps later rounds cheap. The engine used to scrape them out of
prose with a line-anchored regex that tolerated leading whitespace only -- so a verifier whose
house style is markdown (``**VERIFIED: ...**``, ``- `VERIFIED: ...` ``) yielded *nothing*,
silently. Every round then re-derived everything, the lead re-planned work it had already had
done, and the writer re-established a "verified state" it was supposed to be handed. One
unparsed line cost more than any other defect in the tool.

So the contract is now explicit and layered:

1. roles are asked for a machine-readable block (```` ```claims ```` + a JSON list);
2. failing that, a decoration-tolerant line scan recovers statuses from ordinary prose;
3. a report that *mentions* a status while parsing to nothing is an alarm, not a zero --
   ``unharvested()`` is what the engine trips on.

Never let load-bearing data leave through a parser that fails quietly.
"""

from __future__ import annotations

import json
import re

STATUSES = ("verified", "refuted", "unclear")

#: Asked of every verifier/reviewer, and of any role whose output the engine harvests.
BLOCK_INSTRUCTION = (
    "End your reply with a machine-readable block, exactly this shape:\n"
    "```claims\n"
    '[{"status": "verified", "text": "<what you reproduced>"},\n'
    ' {"status": "unclear",  "text": "<what you could not settle>"}]\n'
    "```\n"
    "`status` is one of verified / refuted / unclear. This block is the round's durable record: "
    "anything absent from it is treated as never verified, and will be re-derived later at full "
    "cost. Prose above the block is for the human; the block is for the machine."
)

_BLOCK = re.compile(r"```(?:claims|json)?[^\S\n]*\n(.*?)```", re.S)

# Bullets, quote markers, list numbers, and bold/italic/code decoration may precede the status
# word; any of `:`, `-`, en/em dash may separate it from the text.
_LINE = re.compile(
    r"""^[\s>*_`+•-]*            # bullet / quote / decoration run
        (?:\d+[.)]\s*)?               # "1." / "2)" list numbering
        [\s>*_`]*                     # more decoration
        (VERIFIED|REFUTED|UNCLEAR)    # the status word
        [\s*_`]*                      # closing decoration
        \s*[:–—-]\s*        # separator
        (.+?)\s*$""",
    re.IGNORECASE | re.VERBOSE)

_MENTION = re.compile(r"\b(VERIFIED|REFUTED|UNCLEAR)\b", re.IGNORECASE)


def parse(text: str) -> list[dict]:
    """Claims as ``[{"status": ..., "text": ...}]``, block first, prose scan as fallback."""
    if not text:
        return []
    return _dedupe(_from_blocks(text) or _from_lines(text))


def mentions(text: str) -> int:
    """How many lines even mention a status word -- the denominator for the alarm."""
    return sum(1 for line in (text or "").splitlines() if _MENTION.search(line))


def unharvested(text: str) -> bool:
    """True when the report talks about statuses but nothing parsed: a broken contract."""
    return bool(mentions(text)) and not parse(text)


def _from_blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for body in _BLOCK.findall(text):
        try:
            data = json.loads(body.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            continue
        for item in data:
            entry = _normalize(item)
            if entry:
                out.append(entry)
    return out


def _normalize(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    status = str(item.get("status") or item.get("verdict") or "").strip().lower()
    if status not in STATUSES:
        return None
    for key in ("text", "claim", "statement", "summary"):
        raw = item.get(key)
        if raw:
            return {"status": status, "text": _clean(str(raw))}
    return None


def _from_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            body = _clean(m.group(2))
            if body:
                out.append({"status": m.group(1).lower(), "text": body})
    return out


def _clean(s: str) -> str:
    s = re.sub(r"\*\*|__", "", s)          # bold markers carry no meaning here
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip("`").strip()[:400]


def _dedupe(entries: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in entries:
        key = (e["status"], e["text"].lower())
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out
