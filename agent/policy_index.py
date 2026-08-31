"""Policy corpus index. Retrieval is keyed on class, tier, and amount band.

Fail closed: if the index is missing or no discount-authority chunk matches,
retrieve() returns None and the caller must send a no-offer template.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import threading

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "config" / "merchant_policy.md"

AMOUNT_UNDER = 5000
AMOUNT_OVER = 25000
LTV_SILVER = 5000
LTV_GOLD = 20000

_KIND_LINE = re.compile(r"^POL-(\d+)\s+(\S+)\s*$")
_FIELD = re.compile(r"^([a-z_]+):\s*(.*)$")


@dataclass(frozen=True)
class PolicyChunk:
    id: str
    kind: str
    failure_class: str | None
    amount_band: str | None
    customer_tier: str | None
    permitted: str
    tone: str
    never_say: tuple[str, ...]
    body: str

    def embed_text(self) -> str:
        bits = [
            self.id, self.kind,
            self.failure_class or "any_class",
            self.amount_band or "any_band",
            self.customer_tier or "any_tier",
            self.permitted, self.tone, self.body,
        ]
        return " ".join(bits)


@dataclass(frozen=True)
class RetrievedPolicy:
    chunk_id: str
    kind: str
    permitted: str
    tone: str
    never_say: tuple[str, ...]
    failure_class: str
    amount_band: str
    customer_tier: str


def amount_band(amount: float) -> str:
    amt = float(amount)
    if amt < AMOUNT_UNDER:
        return "under_5000"
    if amt > AMOUNT_OVER:
        return "over_25000"
    return "5000_to_25000"


def customer_tier(lifetime_value: float) -> str:
    ltv = float(lifetime_value or 0)
    if ltv >= LTV_GOLD:
        return "gold"
    if ltv >= LTV_SILVER:
        return "silver"
    return "standard"


def parse_corpus(text: str) -> list[PolicyChunk]:
    chunks: list[PolicyChunk] = []
    current_id = current_kind = None
    fields: dict[str, str] = {}
    body_lines: list[str] = []

    def flush() -> None:
        if current_id is None:
            return
        never = tuple(
            p.strip()
            for p in fields.get("never_say", "").replace("\n", " ").split(",")
            if p.strip()
        )
        chunks.append(PolicyChunk(
            id=current_id,
            kind=current_kind or "",
            failure_class=_blank(fields.get("failure_class")),
            amount_band=_blank(fields.get("amount_band")),
            customer_tier=_blank(fields.get("customer_tier")),
            permitted=fields.get("permitted", "").strip(),
            tone=fields.get("tone", "").strip(),
            never_say=never,
            body=" ".join(body_lines).strip(),
        ))

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _KIND_LINE.match(line)
        if m:
            flush()
            current_id = f"POL-{m.group(1)}"
            current_kind = m.group(2)
            fields = {}
            body_lines = []
            continue
        if current_id is None:
            continue
        stripped = line.strip()
        fm = _FIELD.match(stripped)
        if fm and fm.group(1) in {
            "failure_class", "amount_band", "customer_tier",
            "permitted", "tone", "never_say",
        }:
            key, val = fm.group(1), fm.group(2).strip()
            if key == "never_say" and val.endswith(","):
                fields[key] = val
            else:
                fields[key] = fields.get(key, "")
                fields[key] = (fields[key] + " " + val).strip() if fields[key] else val
            continue
        if stripped:
            body_lines.append(stripped)
    flush()
    if not chunks:
        raise ValueError(f"no policy chunks in {CORPUS_PATH}")
    return chunks


def _blank(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower()
    if v in {"", "any", "n/a"}:
        return None
    return v


def _matches(chunk: PolicyChunk, failure_class: str, tier: str, band: str) -> bool:
    if chunk.failure_class and chunk.failure_class != failure_class:
        return False
    if chunk.amount_band and chunk.amount_band not in {band, "any"}:
        return False
    if chunk.customer_tier and chunk.customer_tier != tier:
        return False
    return True


class PolicyIndex:
    def __init__(self, chunks: list[PolicyChunk]):
        self.chunks = chunks
        self._broken = False
        self._vectorizer = None
        self._faiss = None
        self._matrix = None
        self._build()

    def _build(self) -> None:
        self._vectorizer = None
        self._faiss = None
        self._matrix = None
        texts = [c.embed_text() for c in self.chunks]
        try:
            import numpy as np
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:
            return
        self._vectorizer = TfidfVectorizer(ngram_range=(1, 2))
        matrix = self._vectorizer.fit_transform(texts).astype("float32")
        dense = np.asarray(matrix.toarray(), dtype=np.float32)
        norms = np.linalg.norm(dense, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        dense = dense / norms
        self._matrix = dense
        try:
            import faiss
            faiss.normalize_L2(dense)
            index = faiss.IndexFlatIP(dense.shape[1])
            index.add(dense)
            self._faiss = index
        except ImportError:
            self._faiss = None

    def search(self, query: str, k: int = 8) -> list[PolicyChunk]:
        if self._broken:
            return []
        if self._vectorizer is None or self._matrix is None:
            return list(self.chunks)
        import numpy as np
        q = self._vectorizer.transform([query]).astype("float32")
        dense = np.asarray(q.toarray(), dtype=np.float32)
        n = np.linalg.norm(dense, axis=1, keepdims=True)
        n[n == 0] = 1.0
        dense = dense / n
        if self._faiss is not None:
            _scores, idxs = self._faiss.search(dense, min(k, len(self.chunks)))
            order = [int(i) for i in idxs[0] if i >= 0]
        else:
            scores = self._matrix @ dense[0]
            order = list(np.argsort(-scores)[:k])
        return [self.chunks[i] for i in order]


_lock = threading.Lock()
_INDEX: PolicyIndex | None = None


def load_chunks(path: Path = CORPUS_PATH) -> list[PolicyChunk]:
    return parse_corpus(path.read_text(encoding="utf-8"))


def get_index() -> PolicyIndex:
    global _INDEX
    with _lock:
        if _INDEX is None:
            _INDEX = PolicyIndex(load_chunks())
        return _INDEX


def reset_index() -> None:
    global _INDEX
    with _lock:
        _INDEX = None


def retrieve(failure_class: str, customer_tier_name: str,
             band: str) -> RetrievedPolicy | None:
    """Return the matching discount-authority (or instrument_update) chunk.

    None means fail closed — caller must use the no-offer template.
    """
    idx = get_index()
    if idx._broken:
        return None
    query = (
        f"{failure_class} {customer_tier_name} {band} "
        f"discount_authority instrument_update"
    )
    ranked = idx.search(query)
    eligible = [
        c for c in ranked
        if c.kind in {"discount_authority", "instrument_update"}
        and _matches(c, failure_class, customer_tier_name, band)
    ]
    if not eligible:
        eligible = [
            c for c in idx.chunks
            if c.kind in {"discount_authority", "instrument_update"}
            and _matches(c, failure_class, customer_tier_name, band)
        ]
    if not eligible:
        return None
    chunk = eligible[0]
    tone = chunk.tone
    for c in idx.chunks:
        if c.kind == "tone" and c.customer_tier == customer_tier_name:
            tone = c.tone or tone
            break
    never: list[str] = []
    for c in idx.chunks:
        if c.kind == "prohibited_claims":
            never.extend(c.never_say)
    return RetrievedPolicy(
        chunk_id=chunk.id,
        kind=chunk.kind,
        permitted=chunk.permitted,
        tone=tone,
        never_say=tuple(never),
        failure_class=failure_class,
        amount_band=band,
        customer_tier=customer_tier_name,
    )


@contextmanager
def broken_index():
    """Demo/test helper: retrieval returns nothing. Generation must fail closed."""
    idx = get_index()
    prev = idx._broken
    idx._broken = True
    try:
        yield
    finally:
        idx._broken = prev
