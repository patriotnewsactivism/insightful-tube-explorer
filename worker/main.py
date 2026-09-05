"""
TubeScribe: Audio Worker v16
Pipeline: Pasted transcript → Deepgram Nova-2 (audio) → Supadata API → YouTube captions fallback
         → AI insights (parallel)

v6: Supadata API integration
v7: Speaker ID, polished transcript, AI chat, export
v8: Fact extraction, entity extraction, cross-video search, bulk support
v8.1: Quote extraction, timeline builder, contradiction detector
v9: Replaced Azure AI with free alternatives (Google Gemini + Groq)
v12: Cerebras support (1M tokens/day free), optimized prompt sizes, robust rate limiting
v13: Deepgram Nova-2 as primary transcription (real audio, not captions); fixed raw/polished labeling
v14: Forensic evidence foundation — SHA-256 hash chain, chain of custody log, raw transcript
     preservation, forensic export, hash verification endpoint
v15: Legal precision batch 1 — plain-text sanitizer, confidence scoring, legal entity extraction
v16: Multi-provider quality-first routing with graceful degradation
v17: Cleanup — dead providers (Qwen, Poolside) removed; chain is now Mistral →
     Cerebras → OpenRouter (free) → Groq. Added: pending-analysis poller (works
     even with no Supabase webhook), Stripe checkout/confirm/webhook endpoints
     (monthly/yearly/lifetime plans), admin /status diagnostics.
"""

import os, json, time, hmac, hashlib, base64, tempfile, subprocess, re, uuid, io, html as html_mod, threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.request import urlopen, Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookiejar import MozillaCookieJar
from concurrent.futures import ThreadPoolExecutor

# ── Env vars ────────────────────────────────────────────────────────────────
SUPABASE_URL             = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE    = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
# ── AI provider config (Cerebras: 1M tokens/day free, gpt-oss-120b) ──────────
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL   = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
# ── OpenRouter (free tier: 27 free models, Google login) ───────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
# ── Groq (fallback — free tier: 100K tokens/day) ───────────────────────────
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL       = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACK    = os.environ.get("GROQ_FALLBACK_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
# ── Mistral (api.mistral.ai — high quality, generous limits) ───────────────
MISTRAL_API_KEY  = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL    = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
MISTRAL_BASE_URL = os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1/chat/completions")
USE_GROQ         = bool(GROQ_API_KEY)
USE_CEREBRAS     = bool(CEREBRAS_API_KEY)
USE_OPENROUTER   = bool(OPENROUTER_API_KEY)
USE_MISTRAL      = bool(MISTRAL_API_KEY)
# Which provider to try first (used for the legacy health-endpoint report)
AI_PROVIDER      = ("mistral" if USE_MISTRAL else
                    "mistral" if USE_MISTRAL else
                    "cerebras" if USE_CEREBRAS else
                    "openrouter" if USE_OPENROUTER else
                    "groq")
SUPADATA_API_KEY         = os.environ.get("SUPADATA_API_KEY", "")
# ── Supadata key rotation (multiple keys, auto-rotate on quota exhaustion) ──
# Keys may be comma-separated in SUPADATA_API_KEY and/or numbered
# SUPADATA_API_KEY_1, SUPADATA_API_KEY_2, … When a key hits its allotment
# (HTTP 429/402 or a quota/error message), it is parked for a 1h cooldown and
# the next key is used automatically.
_sd_keys = [k.strip() for k in SUPADATA_API_KEY.split(",") if k.strip()] if SUPADATA_API_KEY else []
_sd_n = 1
while os.environ.get(f"SUPADATA_API_KEY_{_sd_n}"):
    _sd_k = os.environ[f"SUPADATA_API_KEY_{_sd_n}"].strip()
    if _sd_k and _sd_k not in _sd_keys:
        _sd_keys.append(_sd_k)
    _sd_n += 1
SUPADATA_KEYS = _sd_keys               # unique keys (list; empty = not configured)
_supadata_exhausted = {}               # {key: expiry_unix_ts} — parked keys
_supadata_lock = threading.Lock()
DEEPGRAM_API_KEY         = os.environ.get("DEEPGRAM_API_KEY", "")
# ── Stripe monetization (monthly / yearly / lifetime) ────────────────────────
STRIPE_SECRET_KEY    = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTHLY  = os.environ.get("STRIPE_PRICE_MONTHLY",  "price_1UCA59LqbDDXxFmBPnbI3yz9")  # $9/mo
STRIPE_PRICE_YEARLY   = os.environ.get("STRIPE_PRICE_YEARLY",   "price_1UCA5ALqbDDXxFmBIUSuRQax")  # $79/yr
STRIPE_PRICE_LIFETIME = os.environ.get("STRIPE_PRICE_LIFETIME", "price_1UCA5ALqbDDXxFmBldgZch3Y")  # $199 once
PORT                     = int(os.environ.get("PORT", 8080))

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# ── Supabase helpers ─────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

def sb_patch(table, match, data):
    params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    req = Request(url, data=json.dumps(data).encode(), headers=sb_headers(), method="PATCH")
    try:
        urlopen(req)
    except HTTPError as e:
        print(f"[sb_patch] {e.status}: {e.read()}")

def sb_get(table, match, select="*"):
    params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}&select={select}"
    headers = {**sb_headers(), "Prefer": ""}
    req = Request(url, headers=headers)
    try:
        return json.loads(urlopen(req).read())
    except Exception as e:
        print(f"[sb_get] {e}")
        return []

def set_status(analysis_id, status, extra={}):
    sb_patch("analyses", {"id": analysis_id}, {"status": status, **extra})

def fail_analysis(analysis_id, message):
    print(f"[worker] FAILED {analysis_id}: {message}")
    sb_patch("analyses", {"id": analysis_id}, {"status": "failed", "error_message": message[:2000]})

def sb_insert(table, rows):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**sb_headers(), "Prefer": "return=minimal"}
    req = Request(url, data=json.dumps(rows).encode(), headers=headers, method="POST")
    try:
        urlopen(req)
    except HTTPError as e:
        print(f"[sb_insert] {e.status}: {e.read()}")

WORKER_VERSION = "v17"

# ── Legal Precision: Plain-Text Sanitizer ────────────────────────────────────

def sanitize_for_legal(text):
    """Remove all markdown formatting for legal document compatibility.

    Converts AI output to clean plain text suitable for court filings:
    - Strips markdown headers (# ## ###)
    - Converts bold/italic markers to plain text
    - Removes code blocks and backticks
    - Converts markdown lists to plain text with indentation
    - Preserves paragraph structure and timestamps
    """
    if not text:
        return text
    # Strip markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold markers (**text** or __text__)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Remove italic markers (*text* or _text_) — careful not to hit list bullets
    text = re.sub(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)', r'\1', text)
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove blockquotes
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    # Convert markdown bullet lists to plain text
    text = re.sub(r'^[-*+]\s+', '  - ', text, flags=re.MULTILINE)
    # Keep numbered lists but normalize
    text = re.sub(r'^(\d+)\.\s+', r'  \1. ', text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Remove markdown links, keep text: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove markdown images
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Legal Precision: Confidence Score Computation ────────────────────────────

def compute_confidence_score(claim_text, source_type="deepgram_nova2", speaker_identified=True, corroboration_count=0):
    """Compute a confidence score (0.0-1.0) for a factual claim.

    Factors:
    - Source reliability: audio transcription > captions > pasted
    - Speaker identification confidence
    - Hedging language detection
    - Corroboration from multiple mentions
    """
    score = 0.5  # baseline

    # Source reliability bonus
    source_bonuses = {
        "deepgram_nova2": 0.2,
        "supadata_api": 0.1,
        "youtube_captions": 0.05,
        "pasted_transcript": 0.0,
    }
    score += source_bonuses.get(source_type, 0.0)

    # Speaker identification
    if speaker_identified:
        score += 0.1

    # Hedging language penalty
    hedging_terms = ["maybe", "i think", "possibly", "might", "could be",
                     "not sure", "i believe", "probably", "apparently",
                     "allegedly", "reportedly", "it seems"]
    claim_lower = (claim_text or "").lower()
    hedging_count = sum(1 for term in hedging_terms if term in claim_lower)
    score -= min(hedging_count * 0.08, 0.25)

    # Corroboration bonus
    score += min(corroboration_count * 0.05, 0.15)

    return round(max(0.0, min(1.0, score)), 2)


# ── Legal Precision: Legal Entity Extraction ─────────────────────────────────

# Regex patterns for common legal references
LEGAL_PATTERNS = {
    "case_number_federal": re.compile(r'\b\d{1,2}:\d{2}-[a-z]{2}-\d{4,6}\b', re.IGNORECASE),
    "case_number_state": re.compile(r'\b(?:Case|No\.?|Cause)\s*(?:No\.?\s*)?[\d\-]+[A-Z]*\d*\b', re.IGNORECASE),
    "reporter_citation": re.compile(r'\b\d+\s+(?:U\.?S\.?|S\.?\s*Ct\.?|L\.?\s*Ed\.?|F\.?\s*(?:2d|3d|4th)?|So\.?\s*(?:2d|3d)?)\s+\d+\b'),
    "us_code": re.compile(r'\b\d+\s+U\.?S\.?C\.?\s*§\s*\d+[a-z]?\b'),
    "cfr": re.compile(r'\b\d+\s+C\.?F\.?R\.?\s*§?\s*\d+(?:\.\d+)?\b'),
    "state_code": re.compile(r'\b(?:Cal|Tex|Fla|N\.?Y|Ohio|Ill)\.?\s*(?:Civ|Pen|Gov|Fam|Prob|Bus|Lab|Health|Educ)\.?\s*(?:Code|Proc)\.?\s*§\s*\d+\b', re.IGNORECASE),
    "constitutional": re.compile(r'\b(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth)\s+Amendment\b', re.IGNORECASE),
    "court": re.compile(r'\b(?:Supreme Court|District Court|Circuit Court|Court of Appeals|Superior Court|Municipal Court|Family Court|Bankruptcy Court|Tax Court)\b', re.IGNORECASE),
}


def extract_legal_entities_regex(text):
    """Extract legal entities using regex patterns. Returns list of dicts."""
    if not text:
        return []
    entities = []
    seen = set()
    for entity_type, pattern in LEGAL_PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group().strip()
            key = (entity_type, value.lower())
            if key not in seen:
                seen.add(key)
                # Map regex pattern name to entity_type
                if entity_type.startswith("case_number"):
                    etype = "case_number"
                elif entity_type in ("us_code", "cfr", "state_code"):
                    etype = "statute"
                elif entity_type == "reporter_citation":
                    etype = "case_citation"
                elif entity_type == "constitutional":
                    etype = "constitutional_provision"
                elif entity_type == "court":
                    etype = "court"
                else:
                    etype = entity_type
                entities.append({
                    "entity_type": etype,
                    "name": value,
                    "source": "regex",
                    "context": text[max(0, match.start()-50):match.end()+50],
                })
    return entities


def save_legal_entities(user_id, analysis_id, legal_entities):
    """Save extracted legal entities to the legal_entities table."""
    if not legal_entities or not user_id:
        return
    try:
        # Fetch existing to deduplicate
        existing = sb_get("legal_entities", {"user_id": user_id}, "id,name,entity_type")
        existing_keys = {(e["name"].lower(), e["entity_type"]) for e in (existing or [])}

        rows = []
        for ent in legal_entities:
            name = ent.get("name", "").strip()
            etype = ent.get("entity_type", "unknown")
            if not name or (name.lower(), etype) in existing_keys:
                continue
            existing_keys.add((name.lower(), etype))
            rows.append({
                "user_id": user_id,
                "entity_type": etype,
                "name": name,
                "jurisdiction": ent.get("jurisdiction"),
                "full_citation": ent.get("full_citation"),
                "description": ent.get("description", ""),
                "first_seen_analysis": analysis_id,
                "confidence_score": ent.get("confidence_score", 0.5),
                "metadata": {"source": ent.get("source", "regex"), "context": ent.get("context", "")},
            })
        if rows:
            for i in range(0, len(rows), 50):
                sb_insert("legal_entities", rows[i:i+50])
            print(f"[legal] Saved {len(rows)} legal entities for {analysis_id}")
    except Exception as e:
        print(f"[legal] Save error: {e}")


# ── Forensic Evidence Helpers ────────────────────────────────────────────────

def compute_evidence_hash(raw_text, video_id, captured_at_iso):
    """Compute SHA-256 hash of raw transcript + video ID + capture timestamp.

    This creates a tamper-evident fingerprint: if any byte of the original
    transcript changes, the hash will no longer match.
    """
    payload = f"{video_id or 'no-video-id'}|{captured_at_iso}|{raw_text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log_custody(analysis_id, action, details=None, user_id=None, actor=None):
    """Append an immutable entry to the chain of custody log.

    Actions: created, transcript_captured, ai_processed, enrichment_complete,
             exported, forensic_exported, hash_verified, edited, reprocessed
    """
    if actor is None:
        actor = f"worker_{WORKER_VERSION}"
    entry = {
        "analysis_id": analysis_id,
        "action": action,
        "details": details or {},
        "actor": actor,
    }
    if user_id:
        entry["user_id"] = user_id
    sb_insert("custody_log", [entry])


def preserve_raw_transcript(analysis_id, raw_text, video_id, source, user_id=None):
    """Freeze the byte-exact raw transcript and compute forensic hashes.

    Called once, immediately after transcript extraction, before any AI processing.
    Returns (evidence_hash, captured_at) for downstream use.
    """
    captured_at = datetime.now(timezone.utc).isoformat()
    evidence_hash = compute_evidence_hash(raw_text, video_id, captured_at)
    raw_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    capture_metadata = {
        "source": source,
        "video_id": video_id,
        "captured_at": captured_at,
        "worker_version": WORKER_VERSION,
        "hash_algorithm": "sha-256",
        "raw_char_count": len(raw_text),
        "raw_word_count": len(raw_text.split()),
    }

    sb_patch("analyses", {"id": analysis_id}, {
        "evidence_hash": evidence_hash,
        "captured_at": captured_at,
        "capture_metadata": capture_metadata,
        "preserved_raw_transcript": raw_text,
        "preserved_raw_hash": raw_hash,
    })

    log_custody(analysis_id, "transcript_captured", {
        "source": source,
        "evidence_hash": evidence_hash,
        "raw_hash": raw_hash,
        "char_count": len(raw_text),
        "word_count": len(raw_text.split()),
    }, user_id=user_id)

    print(f"[forensic] Evidence preserved for {analysis_id}: hash={evidence_hash[:16]}… source={source}")
    return evidence_hash, captured_at


def verify_evidence_hash(analysis_id):
    """Re-compute and compare the evidence hash. Returns verification result dict."""
    rows = sb_get("analyses", {"id": analysis_id},
                  "preserved_raw_transcript,preserved_raw_hash,evidence_hash,captured_at,capture_metadata")
    if not rows:
        return {"verified": False, "error": "Analysis not found"}
    a = rows[0]

    if not a.get("preserved_raw_transcript") or not a.get("evidence_hash"):
        return {"verified": False, "error": "No forensic data — analysis predates forensic system"}

    video_id = (a.get("capture_metadata") or {}).get("video_id", "no-video-id")

    # Verify the evidence hash (transcript + video_id + timestamp)
    recomputed_evidence = compute_evidence_hash(
        a["preserved_raw_transcript"], video_id, a["captured_at"]
    )
    evidence_match = recomputed_evidence == a["evidence_hash"]

    # Verify the raw transcript hash independently
    recomputed_raw = hashlib.sha256(a["preserved_raw_transcript"].encode("utf-8")).hexdigest()
    raw_match = recomputed_raw == a.get("preserved_raw_hash", "")

    verified = evidence_match and raw_match

    log_custody(analysis_id, "hash_verified", {
        "evidence_match": evidence_match,
        "raw_match": raw_match,
        "verified": verified,
    })

    return {
        "verified": verified,
        "evidence_hash": {"original": a["evidence_hash"], "recomputed": recomputed_evidence, "match": evidence_match},
        "raw_hash": {"original": a.get("preserved_raw_hash"), "recomputed": recomputed_raw, "match": raw_match},
        "captured_at": a.get("captured_at"),
        "capture_metadata": a.get("capture_metadata"),
    }


def get_custody_chain(analysis_id):
    """Retrieve the full chain of custody for an analysis, ordered chronologically."""
    url = (f"{SUPABASE_URL}/rest/v1/custody_log"
           f"?analysis_id=eq.{analysis_id}&order=created_at.asc&select=*")
    headers = {**sb_headers(), "Prefer": ""}
    req = Request(url, headers=headers)
    try:
        return json.loads(urlopen(req).read())
    except Exception as e:
        print(f"[forensic] Custody chain fetch error: {e}")
        return []


def handle_forensic_export(data):
    """Generate a court-ready forensic evidence package for an analysis."""
    analysis_id = data.get("analysis_id")
    if not analysis_id:
        return {"error": "analysis_id required"}

    rows = sb_get("analyses", {"id": analysis_id})
    if not rows:
        return {"error": "Analysis not found"}
    a = rows[0]

    if not a.get("evidence_hash"):
        return {"error": "No forensic data available — analysis predates forensic system"}

    # Get chain of custody
    custody = get_custody_chain(analysis_id)

    # Verify integrity before export
    verification = verify_evidence_hash(analysis_id)

    # Log the export event
    log_custody(analysis_id, "forensic_exported", {
        "integrity_verified": verification.get("verified", False),
        "export_format": "text",
    }, user_id=data.get("user_id"))

    # Build the forensic certificate
    meta = a.get("capture_metadata") or {}
    sections = []

    sections.append("═" * 72)
    sections.append("         CERTIFICATE OF DIGITAL EVIDENCE EXTRACTION")
    sections.append("                        TubeScribe Forensic")
    sections.append("═" * 72)
    sections.append("")
    sections.append(f"  Source URL:         {a.get('youtube_url', 'N/A')}")
    sections.append(f"  Video ID:          {meta.get('video_id', 'N/A')}")
    sections.append(f"  Video Title:       {a.get('title', 'N/A')}")
    sections.append(f"  Channel:           {a.get('channel', 'N/A')}")
    sections.append(f"  Extraction Date:   {a.get('captured_at', 'N/A')}")
    sections.append(f"  Extraction Method: {meta.get('source', 'N/A')}")
    sections.append(f"  Worker Version:    {meta.get('worker_version', 'N/A')}")
    sections.append(f"  Character Count:   {meta.get('raw_char_count', 'N/A')}")
    sections.append(f"  Word Count:        {meta.get('raw_word_count', 'N/A')}")
    sections.append("")
    sections.append("─" * 72)
    sections.append("  INTEGRITY VERIFICATION")
    sections.append("─" * 72)
    sections.append(f"  Hash Algorithm:    SHA-256")
    sections.append(f"  Evidence Hash:     {a.get('evidence_hash', 'N/A')}")
    sections.append(f"  Raw Text Hash:     {a.get('preserved_raw_hash', 'N/A')}")
    v = verification
    status = "✓ VERIFIED — transcript has not been modified since capture" if v.get("verified") else "✗ VERIFICATION FAILED — data may have been modified"
    sections.append(f"  Integrity Status:  {status}")
    sections.append("")
    sections.append("─" * 72)
    sections.append("  CHAIN OF CUSTODY")
    sections.append("─" * 72)
    for entry in custody:
        ts = entry.get("created_at", "")[:19].replace("T", " ")
        action = entry.get("action", "unknown")
        actor = entry.get("actor", "unknown")
        details = entry.get("details", {})
        detail_str = ""
        if isinstance(details, dict):
            # Show key details without being overwhelming
            important = {k: v for k, v in details.items() if k in (
                "source", "evidence_hash", "char_count", "word_count",
                "integrity_verified", "export_format", "verified", "match",
                "note_length", "primary_model"
            )}
            if important:
                detail_str = " | " + ", ".join(f"{k}={v}" for k, v in important.items())
        sections.append(f"  [{ts}] {action} (by {actor}){detail_str}")
    sections.append("")
    sections.append("─" * 72)
    sections.append("  RAW TRANSCRIPT (UNMODIFIED)")
    sections.append("  The text below is the byte-exact original as captured.")
    sections.append("─" * 72)
    sections.append(a.get("preserved_raw_transcript", a.get("polished_transcript", "N/A")))
    sections.append("")
    sections.append("─" * 72)
    sections.append("  AI-ENHANCED TRANSCRIPT (FOR REFERENCE ONLY)")
    sections.append("  DISCLAIMER: This section has been processed by AI for readability.")
    sections.append("  It is NOT the original evidence. Use the raw transcript above for")
    sections.append("  evidentiary purposes.")
    sections.append("─" * 72)
    sections.append(a.get("polished_transcript", "N/A"))
    sections.append("")
    sections.append("═" * 72)
    sections.append("  VERIFICATION INSTRUCTIONS")
    sections.append("═" * 72)
    sections.append("  To independently verify the integrity of this evidence:")
    sections.append("  1. Extract the RAW TRANSCRIPT section above")
    sections.append(f"  2. Compute: SHA-256(\"{meta.get('video_id', 'VIDEO_ID')}|{a.get('captured_at', 'TIMESTAMP')}|<raw_text>\")")
    sections.append(f"  3. The result must match: {a.get('evidence_hash', 'N/A')}")
    sections.append("  4. Independently, SHA-256 of just the raw text must match:")
    sections.append(f"     {a.get('preserved_raw_hash', 'N/A')}")
    sections.append("═" * 72)

    return {
        "text": "\n".join(sections),
        "title": f"Forensic Evidence - {a.get('title', 'export')}",
        "verification": verification,
        "custody_chain": custody,
    }


def handle_verify_hash(data):
    """Public endpoint to verify the integrity of an analysis's evidence hash."""
    analysis_id = data.get("analysis_id")
    if not analysis_id:
        return {"error": "analysis_id required"}
    return verify_evidence_hash(analysis_id)


def handle_custody_log(data):
    """Public endpoint to retrieve the chain of custody for an analysis."""
    analysis_id = data.get("analysis_id")
    if not analysis_id:
        return {"error": "analysis_id required"}
    return {"chain": get_custody_chain(analysis_id)}


# ── YouTube helpers ──────────────────────────────────────────────────────────
def extract_video_id(url):
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

# ── Supadata API (primary transcript source, with key rotation) ─────────────

def _is_supadata_quota_exhausted(http_status, data):
    """Detect whether a Supadata response signals quota/allotment exhaustion."""
    if http_status in (429, 402):
        return True
    if not isinstance(data, dict):
        return False
    err_bits = []
    for field in ("error", "message", "detail"):
        v = data.get(field)
        if isinstance(v, str):
            err_bits.append(v.lower())
        elif isinstance(v, dict):
            err_bits.append((v.get("message", "") + " " + str(v.get("code", ""))).lower())
        elif isinstance(v, list):
            err_bits.append(" ".join(str(e) for e in v).lower())
    blob = " ".join(err_bits)
    quota_terms = ("quota", "rate limit", "limit exceeded", "exceeded", "allotment",
                   "usage limit", "monthly", "too many requests", "credits",
                   "insufficient", "plan limit")
    return any(term in blob for term in quota_terms)


def _supadata_active_key():
    """Return (key, index) of the first non-exhausted Supadata key, or (None, -1)."""
    now = time.time()
    for idx, key in enumerate(SUPADATA_KEYS):
        exp = _supadata_exhausted.get(key)
        if exp is None or exp <= now:
            return key, idx
    return None, -1


def _supadata_request(url, key):
    """Single Supadata curl call with one key. Returns (data, http_status, exhausted)."""
    cmd = [
        "curl", "-s", "-m", "30",
        "-w", "\n__HTTP__%{http_code}",
        "-H", f"x-api-key: {key}",
        "-H", "Accept: application/json",
        "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        if result.returncode != 0:
            print(f"[supadata] curl failed (rc={result.returncode}): {result.stderr[:200]}")
            return None, 0, False
        output = result.stdout.strip()
        if not output:
            return None, 0, False
        body, _, status_str = output.rpartition("__HTTP__")
        body = body.strip()
        http_status = int(status_str.strip()) if status_str.strip().isdigit() else 0
        if not body:
            return None, http_status, False
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            print(f"[supadata] JSON error: {e}, body: {body[:200]}")
            return None, http_status, False
        return data, http_status, _is_supadata_quota_exhausted(http_status, data)
    except subprocess.TimeoutExpired:
        print("[supadata] curl timed out")
        return None, 0, False
    except Exception as e:
        print(f"[supadata] curl exception: {e}")
        return None, 0, False


def _supadata_curl(url):
    """Call Supadata API via curl with automatic key rotation on quota exhaustion.

    Cloudflare blocks urllib, so we use curl. Tries the active key; if the
    response signals allotment exhaustion, parks that key (1h cooldown), writes
    telemetry to the supadata_key_status table (for APEX monitoring), and retries
    with the next key. Returns parsed JSON or None."""
    if not SUPADATA_KEYS:
        return None
    for _ in range(len(SUPADATA_KEYS)):
        with _supadata_lock:
            key, idx = _supadata_active_key()
            was_exhausted = key in _supadata_exhausted if key else False
        if key is None:
            print("[supadata] All keys exhausted — falling back to captions")
            return None
        data, http_status, exhausted = _supadata_request(url, key)
        if data is not None and not exhausted:
            if was_exhausted:  # was on cooldown, now recovered — log + clear
                with _supadata_lock:
                    _supadata_exhausted.pop(key, None)
                _log_supadata_key_status(key, recovered=True)
            return data
        if exhausted:
            with _supadata_lock:
                _supadata_exhausted[key] = time.time() + 3600  # 1h cooldown
            _log_supadata_key_status(key, exhausted=True, http_status=http_status, data=data)
            print(f"[supadata] Key #{idx + 1} exhausted (HTTP {http_status}); rotating to next")
            continue
        # Non-exhaustion failure (network/parse) — don't penalize the key
        return data
    return None


# ── Supadata key telemetry (for APEX portfolio monitoring) ───────────────────
# Writes non-reversible key fingerprints + health state to the
# supadata_key_status table so APEX can monitor the key pool without holding the
# actual keys (which live in env vars). Fire-and-forget — telemetry failures
# never block the transcript pipeline.

def _supadata_key_fingerprint(key):
    """Return a non-reversible ASCII fingerprint of a Supadata key (last 4 chars).
    Uses ASCII '...' (not the ellipsis char) so it's URL-safe in Supabase queries."""
    return f"...{key[-4:]}" if key and len(key) > 4 else "short"


def _supadata_check_quota(key):
    """Call Supadata GET /me to retrieve plan + credit usage. Returns dict or None.
    Not a billable call, so it works even when the transcript quota is exhausted."""
    if not key:
        return None
    cmd = [
        "curl", "-s", "-m", "15", "-w", "\n__HTTP__%{http_code}",
        "-H", f"x-api-key: {key}",
        "-H", "Accept: application/json",
        "https://api.supadata.ai/v1/me",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return None
        body, _, status_str = output.rpartition("__HTTP__")
        body = body.strip()
        http_status = int(status_str.strip()) if status_str.strip().isdigit() else 0
        if not body:
            return None
        data = json.loads(body)
        if not isinstance(data, dict):
            return None
        # Reject error / unauthorized responses (invalid key, etc.)
        if http_status >= 400 or "error" in data:
            return None
        return data
    except Exception:
        return None


def _log_supadata_key_status(key, exhausted=False, recovered=False, http_status=0, data=None):
    """Upsert Supadata key telemetry into supadata_key_status (fire-and-forget).

    Stores only a fingerprint — never the full key value. Runs in a daemon
    thread so it never blocks the transcript pipeline. Also probes GET /me to
    capture remaining-credits context at the moment of the state change."""
    def _write():
        fp = _supadata_key_fingerprint(key)
        now_iso = datetime.now(timezone.utc).isoformat()
        cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=3600)).isoformat()
        me = _supadata_check_quota(key) if (exhausted or recovered) else None
        try:
            existing = sb_get("supadata_key_status", {"key_fingerprint": fp}, "id,exhausted_count")
            patch = {"worker_version": WORKER_VERSION, "last_checked_at": now_iso}
            if me:
                patch["remaining_credits"] = me
            if exhausted:
                cur = (existing[0].get("exhausted_count") if existing else 0) or 0
                patch.update({
                    "status": "exhausted",
                    "exhausted_until": cooldown_iso,
                    "last_exhausted_at": now_iso,
                    "last_http_status": http_status,
                    "exhausted_count": cur + 1,
                })
                if isinstance(data, dict):
                    err = data.get("error") or data.get("message") or data.get("detail")
                    patch["last_error_message"] = str(err)[:300] if err else None
            elif recovered:
                patch.update({"status": "active", "exhausted_until": None, "last_used_at": now_iso})
            if existing:
                sb_patch("supadata_key_status", {"id": existing[0]["id"]}, patch)
            else:
                row = {
                    "key_fingerprint": fp,
                    "status": "exhausted" if exhausted else "active",
                    "worker_version": WORKER_VERSION,
                    "last_checked_at": now_iso,
                }
                if exhausted:
                    row.update({
                        "exhausted_until": cooldown_iso,
                        "last_exhausted_at": now_iso,
                        "last_http_status": http_status,
                        "exhausted_count": 1,
                    })
                    if isinstance(data, dict):
                        err = data.get("error") or data.get("message") or data.get("detail")
                        row["last_error_message"] = str(err)[:300] if err else None
                if me:
                    row["remaining_credits"] = me
                sb_insert("supadata_key_status", [row])
        except Exception as e:
            print(f"[supadata] telemetry write error: {e}")

    threading.Thread(target=_write, daemon=True).start()


def handle_supadata_status():
    """Live Supadata key-pool health for APEX monitoring (on-demand snapshot).
    Probes GET /me per key for remaining credits + in-memory exhausted state.
    Complements the supadata_key_status table (which the pipeline populates on
    state changes) with a real-time read APEX can hit anytime."""
    now = time.time()
    with _supadata_lock:
        snapshot = list(SUPADATA_KEYS)
        exhausted_copy = dict(_supadata_exhausted)
    keys_out = []
    for idx, key in enumerate(snapshot):
        exp = exhausted_copy.get(key)
        if exp and exp > now:
            status = "exhausted"
            until_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        elif exp:
            status = "recovering"
            until_iso = None
        else:
            status = "active"
            until_iso = None
        me = _supadata_check_quota(key)
        keys_out.append({
            "index": idx + 1,
            "fingerprint": _supadata_key_fingerprint(key),
            "status": status,
            "exhausted_until": until_iso,
            "plan": me.get("plan") if me else None,
            "me": me,
        })
    return {
        "configured_keys": len(SUPADATA_KEYS),
        "active_keys": sum(1 for k in keys_out if k["status"] == "active"),
        "exhausted_keys": sum(1 for k in keys_out if k["status"] == "exhausted"),
        "recovering_keys": sum(1 for k in keys_out if k["status"] == "recovering"),
        "keys": keys_out,
    }


def fetch_supadata_transcript(video_id):
    """Fetch transcript via Supadata API — handles YouTube bot detection bypass."""
    if not SUPADATA_KEYS:
        print("[worker] No Supadata keys configured, skipping")
        return None, None, None

    print(f"[worker] Fetching transcript via Supadata API for {video_id}")

    # Step 1: Get transcript (structured with timestamps) — use videoId param
    transcript_url = f"https://api.supadata.ai/v1/youtube/transcript?videoId={video_id}&lang=en"
    transcript_data = _supadata_curl(transcript_url)
    if transcript_data:
        print(f"[worker] Supadata transcript response type: {type(transcript_data).__name__}")
        if isinstance(transcript_data, dict) and "error" in transcript_data:
            print(f"[worker] Supadata API error: {transcript_data}")
            return None, None, None
    else:
        print("[worker] Supadata transcript fetch failed")
        return None, None, None

    # Step 2: Get video metadata (title, description)
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    video_info_url = f"https://api.supadata.ai/v1/youtube/video?url={quote(yt_url, safe='')}"
    video_info = _supadata_curl(video_info_url) or {}
    if video_info:
        print(f"[worker] Supadata video info: title={video_info.get('title', 'N/A')[:60]}")

    return transcript_data, video_info, "supadata"

def parse_supadata_transcript(transcript_data):
    """Parse Supadata transcript response into segments.
    
    Supadata can return:
    - A list of objects with text/start/duration fields (structured)
    - A string (plain text)
    - An object with 'content' or 'transcript' field
    """
    segments = []

    # Handle different response formats
    if isinstance(transcript_data, list):
        # Structured format: [{text, start/offset, duration}, ...]
        # Detect if values are in milliseconds by checking first item
        is_ms = False
        for item in transcript_data[:5]:
            if isinstance(item, dict):
                val = item.get("offset", item.get("startMs", 0))
                if val and float(val) > 500:
                    is_ms = True
                    break
        for item in transcript_data:
            if isinstance(item, dict):
                text = item.get("text", "").strip()
                if not text:
                    continue
                raw_start = float(item.get("start", item.get("offset", item.get("startMs", 0))))
                raw_dur = float(item.get("duration", item.get("dur", 3000 if is_ms else 3)))
                if is_ms:
                    start = raw_start / 1000.0
                    duration = raw_dur / 1000.0
                else:
                    start = raw_start
                    duration = raw_dur
                segments.append({
                    "text": text,
                    "start": start,
                    "end": start + duration,
                })
        if segments:
            print(f"[worker] Parsed {len(segments)} structured Supadata segments (ms={is_ms})")
            return segments

    elif isinstance(transcript_data, dict):
        # Object with content field
        content = transcript_data.get("content") or transcript_data.get("transcript") or ""
        if isinstance(content, list):
            return parse_supadata_transcript(content)
        if isinstance(content, str) and content.strip():
            transcript_data = content  # Fall through to string handling

    # Plain text handling
    if isinstance(transcript_data, str):
        text = transcript_data.strip()
        if not text:
            return []
        # Split into sentence-like chunks
        sentences = re.split(r'(?<=[.!?])\s+', text)
        if len(sentences) <= 1:
            # Split on long pauses / natural breaks (every ~60 words)
            words = text.split()
            chunk_size = 40
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i:i+chunk_size])
                t = (i / chunk_size) * 10.0
                segments.append({"text": chunk, "start": t, "end": t + 10.0})
        else:
            t = 0.0
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                duration = max(2.0, len(sent.split()) * 0.4)
                segments.append({"text": sent, "start": t, "end": t + duration})
                t += duration
        print(f"[worker] Parsed {len(segments)} text Supadata segments")

    return segments

# ── YouTube Transcript (direct, as fallback) ─────────────────────────────────
def fetch_youtube_transcript(video_id):
    print(f"[worker] Fetching YouTube transcript directly for {video_id}")
    page_url = f"https://www.youtube.com/watch?v={video_id}"
    req = Request(page_url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        resp = urlopen(req, timeout=15)
        page_html = resp.read().decode("utf-8", errors="replace")
        print(f"[worker] Video page: {len(page_html)} chars")
    except Exception as e:
        print(f"[worker] Failed to fetch video page: {e}")
        return None

    if "Sign in to confirm" in page_html and "not a bot" in page_html:
        print("[worker] YouTube bot detection triggered on page fetch")
        return None

    patterns = [
        r'var\s+ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;',
        r'ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;',
    ]
    player_json = None
    for pat in patterns:
        m = re.search(pat, page_html, re.DOTALL)
        if m:
            raw = m.group(1)
            depth = 0
            for i, c in enumerate(raw):
                if c == '{': depth += 1
                elif c == '}': depth -= 1
                if depth == 0:
                    try:
                        player_json = json.loads(raw[:i+1])
                    except json.JSONDecodeError:
                        pass
                    break
            if player_json:
                break

    if not player_json:
        print("[worker] Could not extract player response")
        return None

    ps = player_json.get("playabilityStatus", {})
    if ps.get("status") != "OK":
        print(f"[worker] Playability: {ps.get('status')} - {ps.get('reason', 'N/A')}")
        return None

    captions = player_json.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
    tracks = captions.get("captionTracks", [])
    if not tracks:
        print("[worker] No caption tracks found")
        return None

    en_manual, en_auto = None, None
    for track in tracks:
        lang = track.get("languageCode", "")
        kind = track.get("kind", "")
        if lang.startswith("en"):
            if kind != "asr":
                en_manual = track
            else:
                en_auto = en_auto or track

    chosen = en_manual or en_auto or tracks[0]
    track_url = chosen.get("baseUrl", "")
    if not track_url:
        return None

    kind_label = "manual" if chosen.get("kind") != "asr" else "auto-generated"
    print(f"[worker] Found {kind_label} captions ({chosen.get('languageCode', '?')})")

    if "&fmt=" not in track_url:
        track_url += "&fmt=json3"
    else:
        track_url = re.sub(r'&fmt=[^&]*', '&fmt=json3', track_url)

    req = Request(track_url, headers={"User-Agent": USER_AGENT})
    try:
        data = json.loads(urlopen(req, timeout=15).read())
    except Exception as e:
        print(f"[worker] Failed to fetch transcript data: {e}")
        return None

    segments = []
    for event in data.get("events", []):
        segs = event.get("segs", [])
        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = html_mod.unescape(text)
        if not text or text == "\n":
            continue
        start_ms = event.get("tStartMs", 0)
        duration_ms = event.get("dDurationMs", 0)
        segments.append({
            "text": text,
            "start": start_ms / 1000.0,
            "end": (start_ms + duration_ms) / 1000.0,
        })

    if not segments:
        return None

    merged = []
    buf = {"text": "", "start": 0, "end": 0}
    for seg in segments:
        if not buf["text"]:
            buf = dict(seg)
        elif len(buf["text"]) < 80 and not buf["text"].rstrip().endswith(('.', '!', '?')):
            buf["text"] += " " + seg["text"]
            buf["end"] = seg["end"]
        else:
            merged.append(buf)
            buf = dict(seg)
    if buf["text"]:
        merged.append(buf)

    print(f"[worker] YouTube transcript: {len(merged)} segments ({len(segments)} raw)")
    return merged

# ── Pasted transcript parsing ────────────────────────────────────────────────
def parse_pasted_transcript(text):
    lines = text.strip().split("\n")
    segments = []
    ts_pattern = re.compile(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(.*)')
    timestamped_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = ts_pattern.match(line)
        if m:
            if m.group(3):
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            else:
                secs = int(m.group(1)) * 60 + int(m.group(2))
            text_part = m.group(4).strip()
            if text_part:
                timestamped_lines.append((secs, text_part))

    if len(timestamped_lines) > len(lines) * 0.3:
        for i, (start, text_part) in enumerate(timestamped_lines):
            end = timestamped_lines[i + 1][0] if i + 1 < len(timestamped_lines) else start + 10
            segments.append({"text": text_part, "start": float(start), "end": float(end)})
        print(f"[worker] Parsed pasted transcript: {len(segments)} timestamped segments")
    else:
        full_text = " ".join(line.strip() for line in lines if line.strip())
        sentences = re.split(r'(?<=[.!?])\s+', full_text)
        t = 0.0
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            duration = max(2.0, len(sent.split()) * 0.4)
            segments.append({"text": sent, "start": t, "end": t + duration})
            t += duration
        print(f"[worker] Parsed pasted transcript: {len(segments)} sentence segments (no timestamps)")

    return segments


# ── Deepgram Nova-2 Transcription ─────────────────────────────────────────────
def download_audio(video_id, out_path):
    """Download best audio from YouTube using yt-dlp, convert to mp3 via ffmpeg."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--output", out_path,
        "--no-warnings",
        "--quiet",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    print(f"[worker] Downloading audio for {video_id}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"[worker] yt-dlp failed: {result.stderr[:300]}")
        return False
    if not os.path.exists(out_path):
        # yt-dlp may append extension
        mp3_path = out_path + ".mp3" if not out_path.endswith(".mp3") else out_path
        if os.path.exists(mp3_path):
            os.rename(mp3_path, out_path)
        else:
            # search for any file matching base
            base = out_path.rsplit(".", 1)[0]
            for ext in [".mp3", ".m4a", ".webm", ".opus"]:
                candidate = base + ext
                if os.path.exists(candidate):
                    # convert to mp3
                    subprocess.run(["ffmpeg", "-y", "-i", candidate, out_path],
                                   capture_output=True, timeout=120)
                    os.remove(candidate)
                    break
    if not os.path.exists(out_path):
        print("[worker] Audio file not found after yt-dlp")
        return False
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"[worker] Audio downloaded: {size_mb:.1f}MB")
    return True


def transcribe_deepgram(audio_path):
    """Send audio file to Deepgram Nova-2 for transcription with diarization."""
    if not DEEPGRAM_API_KEY:
        print("[worker] No DEEPGRAM_API_KEY set, skipping Deepgram")
        return None

    file_size = os.path.getsize(audio_path)
    print(f"[worker] Sending {file_size/1024/1024:.1f}MB to Deepgram Nova-2...")

    url = (
        "https://api.deepgram.com/v1/listen"
        "?model=nova-2"
        "&smart_format=true"
        "&diarize=true"
        "&punctuate=true"
        "&paragraphs=true"
        "&utterances=true"
        "&language=en-US"
    )

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    req = Request(url, data=audio_data, headers={
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/mpeg",
    }, method="POST")

    try:
        t0 = time.time()
        res = urlopen(req, timeout=600)
        result = json.loads(res.read())
        elapsed = time.time() - t0
        print(f"[worker] Deepgram transcription completed in {elapsed:.1f}s")
        return result
    except HTTPError as e:
        body = e.read().decode()
        print(f"[worker] Deepgram API error ({e.status}): {body[:300]}")
        return None
    except Exception as e:
        print(f"[worker] Deepgram exception: {e}")
        return None


def parse_deepgram_result(result):
    """Parse Deepgram response into utterances and a raw transcript string."""
    if not result:
        return None, None

    utterances = []
    segments = []

    # Use utterances (diarized) if available
    raw_utterances = result.get("results", {}).get("utterances", [])
    if raw_utterances:
        for u in raw_utterances:
            speaker = f"Speaker {u.get('speaker', 0) + 1}"
            text = u.get("transcript", "").strip()
            start = float(u.get("start", 0))
            end = float(u.get("end", start + 1))
            if text:
                utterances.append({"speaker": speaker, "text": text, "start": start, "end": end})
                segments.append({"text": text, "start": start, "end": end})
        print(f"[worker] Deepgram: {len(utterances)} diarized utterances")
    else:
        # Fallback: use word-level or channel transcript
        channels = result.get("results", {}).get("channels", [])
        if channels:
            alts = channels[0].get("alternatives", [])
            if alts:
                words = alts[0].get("words", [])
                # Group words into ~10-second segments
                if words:
                    chunk_words = []
                    chunk_start = float(words[0].get("start", 0))
                    chunk_end = chunk_start
                    for w in words:
                        chunk_words.append(w.get("punctuated_word", w.get("word", "")))
                        chunk_end = float(w.get("end", chunk_end))
                        if chunk_end - chunk_start >= 10.0 or len(chunk_words) >= 30:
                            text = " ".join(chunk_words).strip()
                            if text:
                                utterances.append({"speaker": "Narrator", "text": text,
                                                   "start": chunk_start, "end": chunk_end})
                                segments.append({"text": text, "start": chunk_start, "end": chunk_end})
                            chunk_words = []
                            chunk_start = chunk_end
                    if chunk_words:
                        text = " ".join(chunk_words).strip()
                        if text:
                            utterances.append({"speaker": "Narrator", "text": text,
                                               "start": chunk_start, "end": chunk_end})
                            segments.append({"text": text, "start": chunk_start, "end": chunk_end})
                    print(f"[worker] Deepgram: {len(utterances)} word-grouped segments (no diarization)")

    if not utterances:
        return None, None

    # Build raw transcript text (timestamped, speaker-labeled)
    raw_lines = []
    for u in utterances:
        m = int(u["start"]) // 60
        s = int(u["start"]) % 60
        ts = f"{m}:{s:02d}"
        raw_lines.append(f"[{ts}] {u['speaker']}: {u['text']}")
    raw_transcript_text = "\n".join(raw_lines)

    return utterances, raw_transcript_text

# ── Azure Blob helpers ───────────────────────────────────────────────────────
def parse_conn_str(cs):
    result = {}
    for part in cs.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k] = v
    return result

def hmac_sha256(key, data):
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()

def upload_blob(file_path, blob_name):
    parsed = parse_conn_str(AZURE_STORAGE_CONN)
    account_key = parsed["AccountKey"]
    with open(file_path, "rb") as f:
        file_data = f.read()
    content_type = "audio/mpeg"
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    string_to_sign = "\n".join([
        "PUT", "", "", str(len(file_data)), "", content_type, "",
        "", "", "", "", "",
        f"x-ms-blob-type:BlockBlob\nx-ms-date:{date}\nx-ms-version:2020-04-08",
        f"/{AZURE_STORAGE_ACCOUNT}/{AZURE_STORAGE_CONTAINER}/{blob_name}",
    ])
    key_bytes = base64.b64decode(account_key)
    sig = base64.b64encode(hmac_sha256(key_bytes, string_to_sign)).decode()
    auth = f"SharedKey {AZURE_STORAGE_ACCOUNT}:{sig}"
    url = f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{AZURE_STORAGE_CONTAINER}/{blob_name}"
    req = Request(url, data=file_data, method="PUT", headers={
        "Authorization": auth, "x-ms-blob-type": "BlockBlob",
        "x-ms-date": date, "x-ms-version": "2020-04-08",
        "Content-Type": content_type, "Content-Length": str(len(file_data)),
    })
    try:
        urlopen(req)
    except HTTPError as e:
        raise RuntimeError(f"Blob upload failed ({e.status}): {e.read()}")

def generate_sas_url(blob_name):
    parsed = parse_conn_str(AZURE_STORAGE_CONN)
    account_key = parsed["AccountKey"]
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    expiry = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    version = "2020-04-08"
    string_to_sign = "\n".join([
        "r", start, expiry,
        f"/{AZURE_STORAGE_ACCOUNT}/{AZURE_STORAGE_CONTAINER}/{blob_name}",
        "", "", "https", version, "b", "", "", "", "", "", "",
    ])
    key_bytes = base64.b64decode(account_key)
    sig = base64.b64encode(hmac_sha256(key_bytes, string_to_sign)).decode()
    qs = urlencode({"sv": version, "st": start, "se": expiry, "sr": "b", "sp": "r", "spr": "https", "sig": sig})
    return f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{AZURE_STORAGE_CONTAINER}/{blob_name}?{qs}"

# ── Azure Speech: Fast Transcription ─────────────────────────────────────────
def parse_iso_duration(s):
    if not s:
        return 0.0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?', s)
    if not m:
        return 0.0
    return int(m.group(1) or 0)*3600 + int(m.group(2) or 0)*60 + float(m.group(3) or 0)

def fast_transcribe(audio_path, analysis_id):
    endpoint = AZURE_SPEECH_ENDPOINT.rstrip("/")
    url = f"{endpoint}/speechtotext/v3.2/transcriptions:transcribe"
    boundary = uuid.uuid4().hex
    definition = json.dumps({
        "locales": ["en-US"],
        "profanityFilterMode": "None",
        "channels": [0, 1],
        "diarization": {"maxSpeakers": 10, "enabled": True}
    })
    with open(audio_path, "rb") as f:
        audio_data = f.read()
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="definition"\r\n')
    body.write(b'Content-Type: application/json\r\n\r\n')
    body.write(definition.encode())
    body.write(f"\r\n--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="audio"; filename="audio.mp3"\r\n'.encode())
    body.write(b'Content-Type: audio/mpeg\r\n\r\n')
    body.write(audio_data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    data = body.getvalue()
    print(f"[worker] Fast transcription: sending {len(data)/1024/1024:.1f}MB")
    req = Request(url, data=data, headers={
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }, method="POST")
    try:
        t0 = time.time()
        res = urlopen(req, timeout=600)
        result = json.loads(res.read())
        print(f"[worker] Fast transcription completed in {time.time()-t0:.1f}s")
        return result
    except HTTPError as e:
        raise RuntimeError(f"Fast transcription failed ({e.status}): {e.read().decode()}")

def parse_fast_utterances(result):
    utterances = []
    for phrase in result.get("phrases", []):
        text = phrase.get("text", "").strip()
        if not text:
            continue
        speaker_num = phrase.get("speaker")
        speaker = f"Speaker {speaker_num}" if speaker_num is not None else "Unknown"
        start = parse_iso_duration(phrase.get("offset", ""))
        duration = parse_iso_duration(phrase.get("duration", ""))
        utterances.append({"speaker": speaker, "text": text, "start": start, "end": start + duration})
    return utterances

# ── AI Model Calls ───────────────────────────────────────────────────────────
CONTENT_FILTER_FALLBACK = "[Content could not be analyzed due to content policy restrictions on the transcript material.]"


def _call_api_once(instructions, input_text, max_tokens, model, provider="groq"):
    """Single API call to any configured provider (all OpenAI-compatible)."""
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if input_text:
        messages.append({"role": "user", "content": input_text})
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    if provider == "mistral":
        url = MISTRAL_BASE_URL
        api_key = MISTRAL_API_KEY
    elif provider == "cerebras":
        url = "https://api.cerebras.ai/v1/chat/completions"
        api_key = CEREBRAS_API_KEY
    elif provider == "openrouter":
        url = "https://openrouter.ai/api/v1/chat/completions"
        api_key = OPENROUTER_API_KEY
    else:
        url = "https://api.groq.com/openai/v1/chat/completions"
        api_key = GROQ_API_KEY
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "TubeScribe/1.0",
        "Accept": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://tubescribe.donmatthews.live"
        headers["X-Title"] = "TubeScribe"
    req = Request(url, data=body, headers=headers, method="POST")
    resp = urlopen(req, timeout=180)
    data = json.loads(resp.read())
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _call_with_retries(instructions, input_text, max_tokens=2000, model=None, provider="groq"):
    """Call AI API with retry on rate-limit (429) and transient errors."""
    import random
    if not model:
        model = CEREBRAS_MODEL if provider == "cerebras" else GROQ_MODEL
    max_retries = 5
    for attempt in range(max_retries):
        try:
            return _call_api_once(instructions, input_text, max_tokens, model, provider)
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            status = e.status if hasattr(e, 'status') else getattr(e, 'code', 0)
            print(f"[_call_{provider}] HTTP {status} ({model}) attempt {attempt+1}/{max_retries}: {err_body[:200]}")
            # Retry on rate-limit (429/413), server errors (500+), and Cloudflare blocks (403/1010)
            if status in (429, 413, 500, 502, 503, 504) or (status == 403 and "1010" in err_body):
                # If daily limit hit, don't waste time retrying
                if "tokens per day" in err_body.lower() or "daily" in err_body.lower():
                    raise RuntimeError(f"{provider} daily token limit reached: {err_body[:200]}")
                wait = (2 ** attempt) + 1 + random.uniform(0, 2)
                print(f"[_call_{provider}] Retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"{provider} HTTP {status}: {err_body}")
        except Exception as e:
            if "daily token limit" in str(e).lower():
                raise
            print(f"[_call_{provider}] Non-HTTP error ({model}) attempt {attempt+1}: {str(e)[:200]}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            raise
    raise RuntimeError(f"{provider} {model} failed after {max_retries} retries")


# Global lock to stagger parallel calls and avoid rate limit bursts
_ai_call_lock = __import__('threading').Lock()
_ai_last_call = [0.0]  # mutable container for last call timestamp

def call_openai(instructions, input_text, max_tokens=2000, _track_model=None):
    """Route to the best available AI provider, trying each in quality-priority
    order with graceful fallback. Staggers parallel calls to avoid rate bursts.
    If _track_model is a list, appends the name of the model that succeeded.

    Chain (when configured): Mistral → Cerebras → OpenRouter → Groq → Groq-fallback. The first provider to return content wins;
    auth/endpoint failures (4xx) fall through to the next provider immediately."""
    # Build the provider chain: best quality first, known-good fallbacks last.
    chain = []
    if USE_MISTRAL:
        chain.append(("mistral", MISTRAL_MODEL))
    if USE_CEREBRAS:
        chain.append(("cerebras", CEREBRAS_MODEL))
    if USE_OPENROUTER:
        chain.append(("openrouter", OPENROUTER_MODEL))
    if USE_GROQ:
        chain.append(("groq", GROQ_MODEL))
        chain.append(("groq", GROQ_FALLBACK))

    if not chain:
        raise RuntimeError("No AI API key set — set MISTRAL_API_KEY, "
                       "CEREBRAS_API_KEY, OPENROUTER_API_KEY, or GROQ_API_KEY")

    # Stagger parallel calls to avoid rate-limit bursts (fast providers 0.3s, Groq 1.5s)
    stagger = 0.3 if AI_PROVIDER not in ("openrouter", "groq") else (0.5 if AI_PROVIDER == "openrouter" else 1.5)
    with _ai_call_lock:
        now = time.time()
        wait_until = _ai_last_call[0] + stagger
        if now < wait_until:
            time.sleep(wait_until - now)
        _ai_last_call[0] = time.time()

    errors = []
    for provider, model in chain:
        try:
            result = _call_with_retries(instructions, input_text, max_tokens, model=model, provider=provider)
            if result:
                if isinstance(_track_model, list):
                    _track_model.append(f"{provider} ({model})")
                return result
            print(f"[call_openai] {provider} returned empty, trying next")
        except Exception as e:
            err_str = str(e)
            errors.append(f"{provider}: {err_str[:200]}")
            # Content filter / moderation from ANY provider → safe fallback immediately
            if "content_filter" in err_str.lower() or "moderation" in err_str.lower():
                print(f"[call_openai] Content filter triggered ({provider}): {err_str[:300]}")
                if isinstance(_track_model, list):
                    _track_model.append("content_filter_fallback")
                return CONTENT_FILTER_FALLBACK
            print(f"[call_openai] {provider} failed: {err_str[:300]}, trying next")

    error_detail = "; ".join(errors) if errors else "No AI providers configured"
    configured = []
    if USE_MISTRAL: configured.append("Mistral")
    if USE_CEREBRAS: configured.append("Cerebras")
    if USE_OPENROUTER: configured.append("OpenRouter")
    if USE_GROQ: configured.append("Groq")
    provider_note = f" Configured providers: {', '.join(configured) or 'NONE'}."
    if "daily token limit" in error_detail.lower() or "tokens per day" in error_detail.lower():
        suggestions = []
        if not USE_MISTRAL:
            suggestions.append("Add MISTRAL_API_KEY (free tier at console.mistral.ai)")
        if not USE_CEREBRAS:
            suggestions.append("Add CEREBRAS_API_KEY (gpt-oss-120b at cerebras.ai)")
        if not USE_GROQ:
            suggestions.append("Add GROQ_API_KEY (free 100K tokens/day at groq.com)")
        fix_hint = " Fix: " + "; ".join(suggestions) if suggestions else ""
        raise RuntimeError(
            f"Daily AI token limit reached.{provider_note} Resets at midnight UTC (~6 PM CST).{fix_hint}"
        )
    raise RuntimeError(f"All AI models failed:{provider_note} {error_detail}")

def get_known_speakers(user_id):
    """Fetch known speakers for this user to help with identification."""
    try:
        rows = sb_get("speakers", {"user_id": user_id}, "id,name,channel,notes")
        return rows if rows else []
    except Exception:
        return []

def _get_notes_prompt(research_ctx, note_length="medium"):
    """Return notes prompt based on desired length: short, medium, or detailed."""
    if note_length == "short":
        return f"""{research_ctx}\n\nProduce concise research notes (~1 page) from this video transcript.

Use these sections (as plain text headers with a line of dashes underneath, no markdown):

KEY TOPICS
(bullet points using dashes)

TOP 3 CLAIMS
(who said what)

PEOPLE MENTIONED
(name and role)

NOTABLE QUOTES
(2-3 most significant)

Keep it brief and scannable."""
    elif note_length == "detailed":
        return f"""{research_ctx}\n\nProduce exhaustive, comprehensive expanded research notes from this video transcript. Extract EVERY piece of useful information. Leave nothing out.

Use these sections (as plain text headers in ALL CAPS with a line of dashes underneath, no markdown):

MAIN TOPICS
(List and explain every topic discussed in depth, not just headlines)

KEY CLAIMS AND ALLEGATIONS
(Every factual claim, allegation, or assertion made — include who said it, when, and any supporting evidence mentioned)

PEOPLE AND ORGANIZATIONS
(Every person and organization mentioned, with their role, what was said about them, and relationships to other mentioned parties)

LEGAL AND OFFICIAL PROCEEDINGS
(Any court cases, filings, hearings, laws, statutes, or official actions referenced — include case numbers if mentioned)

NOTABLE QUOTES
(All significant direct quotes with full speaker attribution and context)

TIMELINE OF EVENTS
(Detailed chronological sequence of all events discussed)

DATA AND STATISTICS
(Any numbers, percentages, dates, or quantitative claims)

ACTION ITEMS AND NEXT STEPS
(Everything mentioned as needing to be done, by whom, and any deadlines)

UNANSWERED QUESTIONS
(All questions raised but not answered in the video)

CROSS-REFERENCES
(Connections to other events, people, or cases mentioned)

Be maximally exhaustive. A researcher using these notes should NEVER need to re-watch the video."""
    else:  # medium (default)
        return f"""{research_ctx}\n\nProduce comprehensive expanded research notes from this video transcript. Be thorough and extract ALL useful information.

Use these sections (as plain text headers in ALL CAPS with a line of dashes underneath, no markdown):

MAIN TOPICS
(List and explain every topic discussed, not just headlines)

KEY CLAIMS AND ALLEGATIONS
(Every factual claim, allegation, or assertion made — include who said it)

PEOPLE AND ORGANIZATIONS
(Every person and organization mentioned, with their role and what was said about them)

LEGAL AND OFFICIAL PROCEEDINGS
(Any court cases, filings, hearings, laws, or official actions referenced)

NOTABLE QUOTES
(Direct quotes that are significant, with speaker attribution)

TIMELINE OF EVENTS
(Chronological sequence of events discussed)

ACTION ITEMS AND NEXT STEPS
(Anything mentioned as needing to be done)

UNANSWERED QUESTIONS
(Questions raised but not answered in the video)

Be exhaustive. A researcher using these notes should not need to re-watch the video."""


def generate_insights(transcript, title, description="", user_id=None, note_length="medium"):
    ctx = f'Video title: "{title}"\n\n' if title else ""
    if description:
        ctx += f'Video description: "{description[:500]}"\n\n'
    t = transcript[:60000]
    # Optimized sizes: not every prompt needs the full transcript
    t_short = transcript[:15000]   # ~3.7K tokens — enough for summary, sentiment, date, speakers
    t_medium = transcript[:30000]  # ~7.5K tokens — for notes
    t_full = transcript[:60000]    # ~15K tokens — only for polished transcript

    # Get known speakers for context
    known_speakers = get_known_speakers(user_id) if user_id else []
    speaker_ctx = ""
    if known_speakers:
        names = ", ".join(s["name"] for s in known_speakers[:20])
        speaker_ctx = f"\n\nKnown speakers from previous videos: {names}. Try to match voices/speakers to these known people if they appear in this video."

    # System preamble for all prompts — helps avoid Azure content filter refusals
    research_ctx = "You are a professional research assistant helping a journalist and author document public records, court proceedings, and civic matters for a nonfiction book. All content is from publicly available YouTube videos. Your role is to accurately transcribe, summarize, and organize this public interest content."

    # Legal precision: instruct all prompts to output plain text
    plain_text_rule = "\n\nIMPORTANT: Output plain text only. Do NOT use markdown formatting — no asterisks, no hash symbols for headers, no backticks, no bold/italic markers. Use plain text with line breaks, indentation, and numbered/bulleted lists (using dashes) for structure. This output will be used in legal documents."

    prompts = [
        # 1st call: Summary (plain text)
        (f"{research_ctx}{plain_text_rule}\n\nProduce a thorough summary of this video. Include: the main topic, all key points discussed, names of people and organizations mentioned, any legal proceedings or events described, and the overall significance. Be detailed — aim for 2-3 paragraphs, not just a few sentences.",
         f"{ctx}Transcript:\n{t_short}"),
        # 2nd call: Sentiment (JSON — no plain_text_rule needed)
        (f'{research_ctx}\n\nAnalyze the tone and return ONLY valid JSON (no markdown): {{"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}}',
         f"{ctx}Transcript:\n{t_short}"),
        # 3rd call: Notes (plain text)
        (_get_notes_prompt(research_ctx, note_length) + plain_text_rule,
         f"{ctx}Transcript:\n{t_medium}"),
        # 4th call: Date estimation (JSON — no plain_text_rule needed)
        (f'{research_ctx}\n\nAnalyze for clues about when this content was produced. Return ONLY valid JSON (no markdown): {{"likely_production_date":"<date range>","reasoning":"<brief>"}}',
         f"{ctx}Transcript:\n{t_short}"),
        # 5th call: Speaker-aware polished transcript (plain text)
        (f'''{research_ctx}{plain_text_rule}

You are an expert transcript editor. Create a polished, readable version of this COMPLETE transcript for research documentation purposes. Do NOT truncate, summarize, or skip any part of the transcript.

Rules:
1. Identify different speakers from context clues (names mentioned, "I", "you", conversation flow, who is recording, etc.)
2. Label each speaker with their likely real name if identifiable, otherwise "Speaker 1", "Speaker 2", etc.
3. Fix obvious transcription errors, grammar issues, and filler words (um, uh, like)
4. Add paragraph breaks at natural topic shifts
5. Keep the meaning 100% accurate — never change what was said, only how it reads
6. Format speaker labels as: SPEAKER NAME: Their dialogue here...
7. Add [timestamp] markers every few paragraphs if timing info is available
8. Include EVERY part of the conversation from start to finish{speaker_ctx}

Return ONLY the polished transcript text, no other commentary. Do not skip or summarize any sections.''',
         f"{ctx}Transcript:\n{transcript[:60000]}"),
        # 6th call: Speaker identification (JSON — no plain_text_rule needed)
        (f'''{research_ctx}

Identify all speakers in this transcript for research indexing. Return ONLY valid JSON (no markdown):
{{"speakers": [{{"label": "Speaker 1", "likely_name": "name or null", "role": "brief role description", "speaking_percentage": 0-100, "key_quotes": ["notable quote 1"]}}]}}

Look for: names mentioned in conversation, self-references, titles, the video creator/recorder.{speaker_ctx}''',
         f"{ctx}Transcript:\n{t_short}"),
    ]

    # Token limits per call: polished transcript & notes get 16K, others get 4K
    notes_tokens = {"short": 2000, "medium": 8000, "detailed": 16000}.get(note_length, 8000)
    # Token budget per call — reduced where possible to stay within free-tier limits.
    # Sentiment/date/speakers return tiny JSON; 500 tokens is plenty.
    token_limits = {0: 3000, 1: 500, 2: notes_tokens, 3: 500, 4: 16000, 5: 1000}

    t0 = time.time()
    # Track which model handled each call
    model_trackers = [[] for _ in prompts]
    # Run max 2 at a time to stay within Groq free-tier token window (12K tokens)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call_openai, p[0], p[1], token_limits.get(i, 4000), model_trackers[i]) for i, p in enumerate(prompts)]
        results = [f.result() for f in futures]
    elapsed = time.time() - t0
    models_used = [t[0] if t else "unknown" for t in model_trackers]
    primary_model = max(set(models_used), key=models_used.count) if models_used else "unknown"
    # Rough token estimate: ~4 chars per token for English text
    est_input_tokens = sum(len(p[0]) + len(p[1]) for p in prompts) // 4
    est_output_tokens = sum(len(r) for r in results) // 4
    est_total_tokens = est_input_tokens + est_output_tokens
    print(f"[worker] 6 AI calls completed in {elapsed:.1f}s (parallel) — models: {models_used}")
    print(f"[worker] Estimated tokens: ~{est_input_tokens:,} input + ~{est_output_tokens:,} output = ~{est_total_tokens:,} total")
    summary, sentiment_raw, notes, date_raw, polished_text, speakers_raw = results

    # Legal precision: sanitize all text outputs to plain text
    summary = sanitize_for_legal(summary)
    notes = sanitize_for_legal(notes)
    polished_text = sanitize_for_legal(polished_text)

    # If transcript is very long, process polished transcript in chunks and combine
    if len(transcript) > 60000 and (not polished_text or len(polished_text) < len(transcript) * 0.3):
        print(f"[worker] Transcript is {len(transcript)} chars, processing polished transcript in chunks...")
        chunk_size = 50000
        chunks = [transcript[i:i+chunk_size] for i in range(0, len(transcript), chunk_size)]
        polished_parts = []
        for ci, chunk in enumerate(chunks):
            part = call_openai(
                f'''{research_ctx}{plain_text_rule}\n\nYou are an expert transcript editor. Polish this section (part {ci+1} of {len(chunks)}) into clean, readable text. Fix grammar, add speaker labels (SPEAKER NAME: format), add paragraph breaks. Do NOT skip or summarize any content. Output ONLY the polished text.{speaker_ctx}''',
                f"{ctx}Transcript section {ci+1}/{len(chunks)}:\n{chunk}",
                16000
            )
            polished_parts.append(part)
        polished_text = "\n\n".join(polished_parts)
        polished_text = sanitize_for_legal(polished_text)
        print(f"[worker] Chunked polished transcript: {len(polished_text)} chars from {len(chunks)} chunks")

    # Detect Azure content filter refusals and retry with softer framing
    REFUSAL_MARKERS = ["cannot assist", "can't assist", "i'm sorry", "i am sorry", "unable to process", "content policy"]
    def is_refusal(text):
        return any(m in (text or "").lower()[:100] for m in REFUSAL_MARKERS)

    retry_prompts = {}
    if is_refusal(polished_text):
        retry_prompts[4] = (f"{research_ctx}\n\nClean up this raw transcript into readable paragraphs. Fix typos and add speaker labels where possible. Output only the cleaned text.", f"{ctx}Raw text:\n{transcript[:60000]}")
    if is_refusal(notes):
        retry_prompts[2] = (f"{research_ctx}\n\nCreate organized research notes from this public video transcript. Sections: Topics Discussed, Key Points, Questions Raised.", f"{ctx}Transcript:\n{t}")
    if is_refusal(summary):
        retry_prompts[0] = (f"{research_ctx}\n\nBriefly summarize the topics discussed in this public video.", f"{ctx}Transcript:\n{t}")

    if retry_prompts:
        print(f"[worker] Retrying {len(retry_prompts)} refusal(s): indices {list(retry_prompts.keys())}")
        with ThreadPoolExecutor(max_workers=len(retry_prompts)) as executor:
            retry_futures = {idx: executor.submit(call_openai, p[0], p[1], token_limits.get(idx, 4000)) for idx, p in retry_prompts.items()}
            for idx, fut in retry_futures.items():
                val = fut.result()
                if not is_refusal(val):
                    val = sanitize_for_legal(val)
                    if idx == 0: summary = val
                    elif idx == 2: notes = val
                    elif idx == 4: polished_text = val
                    print(f"[worker] Retry succeeded for index {idx}")
                else:
                    print(f"[worker] Retry still refused for index {idx}")

    try:
        m = re.search(r"\{[\s\S]*\}", sentiment_raw)
        sentiment = json.loads(m.group()) if m else {"raw": sentiment_raw}
    except Exception:
        sentiment = {"raw": sentiment_raw}

    likely_date, date_reasoning = "", ""
    try:
        m = re.search(r"\{[\s\S]*\}", date_raw)
        if m:
            parsed = json.loads(m.group())
            likely_date = parsed.get("likely_production_date", "")
            date_reasoning = parsed.get("reasoning", "")
    except Exception:
        likely_date, date_reasoning = "Unknown", date_raw

    # Parse speaker identification
    speakers_info = []
    try:
        m = re.search(r"\{[\s\S]*\}", speakers_raw)
        if m:
            speakers_info = json.loads(m.group()).get("speakers", [])
    except Exception:
        pass

    return {
        "summary": summary, "sentiment": sentiment, "expanded_notes": notes,
        "likely_production_date": likely_date, "production_date_reasoning": date_reasoning,
        "polished_transcript": polished_text,
        "speakers_info": speakers_info,
        "ai_model_info": {
            "primary_model": primary_model,
            "models_used": list(set(models_used)),
            "processing_time_seconds": round(elapsed, 1),
            "note_length": note_length,
            "worker_version": WORKER_VERSION,
            "estimated_tokens": est_total_tokens,
        },
        "ai_provenance": {
            "worker_version": WORKER_VERSION,
            "primary_model": primary_model,
            "models_used": list(set(models_used)),
            "processing_time_seconds": round(elapsed, 1),
            "plain_text_sanitized": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }

# ── Pipeline ─────────────────────────────────────────────────────────────────
MAX_FAST_TRANSCRIBE_SIZE = 200 * 1024 * 1024

def run_pipeline(record):
    analysis_id = record["id"]
    youtube_url = record["youtube_url"]
    title = record.get("title")
    description = record.get("description", "")
    user_id = record.get("user_id")
    t_start = time.time()
    print(f"[worker] Starting pipeline for {analysis_id} url={youtube_url}")

    try:
        # ── Check for pasted transcript first ──
        pasted = record.get("pasted_transcript") or ""
        if not pasted:
            rows = sb_get("analyses", {"id": analysis_id}, "pasted_transcript")
            if rows and rows[0].get("pasted_transcript"):
                pasted = rows[0]["pasted_transcript"]

        if pasted and len(pasted.strip()) > 50:
            # ── TRACK 0: Pasted transcript mode ──
            print(f"[worker] Using pasted transcript ({len(pasted)} chars)")
            set_status(analysis_id, "transcribing")

            segments = parse_pasted_transcript(pasted)
            utterances = [
                {"speaker": "Narrator", "text": seg["text"], "start": seg["start"], "end": seg["end"]}
                for seg in segments
            ]
            # raw_transcript_text = joined captions; polished is set by AI below
            raw_transcript_text = "\n".join(seg["text"] for seg in segments)
            raw_data = {"source": "pasted_transcript", "char_count": len(pasted), "segments": len(segments)}
            used_source = "pasted transcript"

        else:
            video_id = extract_video_id(youtube_url)
            if not video_id:
                fail_analysis(analysis_id, "Could not extract video ID from URL")
                return

            utterances = None
            raw_transcript_text = None
            raw_data = {}
            used_source = None

            # ── TRACK 1: Deepgram Nova-2 (real audio transcription — most accurate) ──
            if DEEPGRAM_API_KEY:
                set_status(analysis_id, "extracting")
                with tempfile.TemporaryDirectory() as tmpdir:
                    audio_path = os.path.join(tmpdir, "audio.mp3")
                    if download_audio(video_id, audio_path):
                        set_status(analysis_id, "transcribing")
                        dg_result = transcribe_deepgram(audio_path)
                        dg_utterances, dg_raw_text = parse_deepgram_result(dg_result)
                        if dg_utterances and dg_raw_text:
                            utterances = dg_utterances
                            raw_transcript_text = dg_raw_text
                            raw_data = {
                                "source": "deepgram_nova2",
                                "segments": len(dg_utterances),
                                "diarized": any(u["speaker"] != "Narrator" for u in dg_utterances),
                            }
                            used_source = "Deepgram Nova-2"
                            print(f"[worker] Deepgram track complete: {len(utterances)} utterances")
                        else:
                            print("[worker] Deepgram returned no results, falling back")
                    else:
                        print("[worker] Audio download failed, falling back to captions")

            # ── TRACK 2: Supadata API (caption-based fallback) ──
            if utterances is None:
                set_status(analysis_id, "extracting")
                transcript_data, video_info, src = fetch_supadata_transcript(video_id)

                if transcript_data:
                    set_status(analysis_id, "transcribing")
                    segments = parse_supadata_transcript(transcript_data)

                    # Update title/description from Supadata if not already set
                    if video_info:
                        if not title and video_info.get("title"):
                            title = video_info["title"]
                            sb_patch("analyses", {"id": analysis_id}, {"title": title})
                        if not description and video_info.get("description"):
                            description = video_info["description"]

                    if segments and len(segments) > 0:
                        print(f"[worker] Using Supadata transcript ({len(segments)} segments)")
                        utterances = [
                            {"speaker": "Narrator", "text": seg["text"], "start": seg["start"], "end": seg["end"]}
                            for seg in segments
                        ]
                        raw_transcript_text = "\n".join(seg["text"] for seg in segments)
                        raw_data = {"source": "supadata_api", "segments": len(segments),
                                    "video_info": video_info if video_info else {}}
                        used_source = "Supadata API"
                    else:
                        if isinstance(transcript_data, str) and len(transcript_data.strip()) > 50:
                            text = transcript_data.strip()
                            utterances = [{"speaker": "Narrator", "text": text, "start": 0.0, "end": len(text.split()) * 0.4}]
                            raw_transcript_text = text
                            raw_data = {"source": "supadata_api_text", "char_count": len(text)}
                            used_source = "Supadata API (plain text)"

            # ── TRACK 3: YouTube captions (direct scrape) ──
            if utterances is None:
                yt_transcript = fetch_youtube_transcript(video_id) if video_id else None

                if yt_transcript and len(yt_transcript) > 5:
                    print(f"[worker] Using YouTube captions ({len(yt_transcript)} segments)")
                    set_status(analysis_id, "transcribing")
                    utterances = [
                        {"speaker": "Narrator", "text": seg["text"], "start": seg["start"], "end": seg["end"]}
                        for seg in yt_transcript
                    ]
                    raw_transcript_text = "\n".join(seg["text"] for seg in yt_transcript)
                    raw_data = {"source": "youtube_captions", "segments": len(yt_transcript)}
                    used_source = "YouTube captions"

            # ── TRACK 4: Fail ──
            if utterances is None:
                if SUPADATA_KEYS or DEEPGRAM_API_KEY:
                    fail_analysis(analysis_id,
                        "Could not extract transcript. Audio download may have failed and "
                        "no captions are available. Try the 'Paste transcript' option.")
                else:
                    fail_analysis(analysis_id,
                        "YouTube blocks server access for transcript extraction. "
                        "Please use the 'Paste transcript manually' option on the dashboard.")
                return

        # ── Forensic: Preserve raw transcript & compute evidence hash ──
        video_id = extract_video_id(youtube_url)
        evidence_hash, captured_at = preserve_raw_transcript(
            analysis_id, raw_transcript_text, video_id, used_source, user_id=user_id
        )

        # ── AI Insights ──
        set_status(analysis_id, "processing")
        log_custody(analysis_id, "ai_processing_started", {
            "transcript_chars": len(raw_transcript_text),
        }, user_id=user_id)
        # Pass raw_transcript_text to AI for polishing/analysis
        insights = generate_insights(raw_transcript_text, title, description, user_id=user_id)

        # Extract non-column fields before saving
        speakers_info = insights.pop("speakers_info", [])
        ai_model_info = insights.pop("ai_model_info", {})
        ai_provenance = insights.pop("ai_provenance", {})

        # If Deepgram was used and provided real speaker diarization, prefer those speakers
        # over the LLM-guessed ones (LLM speakers_info still useful for names/roles)
        if raw_data.get("source") == "deepgram_nova2" and raw_data.get("diarized"):
            dg_speakers = list({u["speaker"] for u in utterances if u["speaker"] != "Narrator"})
            if dg_speakers and not speakers_info:
                speakers_info = [{"label": sp, "likely_name": sp} for sp in dg_speakers]

        # ── Save ──
        # polished_transcript = AI-polished version (readable prose)
        # raw_transcript field (JSONB) = source metadata + raw caption/audio text
        sb_patch("analyses", {"id": analysis_id}, {
            "status": "complete",
            "raw_transcript": {
                **raw_data,
                "raw_text": raw_transcript_text,
                "speakers_info": speakers_info,
                "ai_model_info": ai_model_info,
            },
            "ai_provenance": ai_provenance,
            **insights,
        })

        log_custody(analysis_id, "ai_processed", {
            "primary_model": ai_model_info.get("primary_model", "unknown"),
            "models_used": ai_model_info.get("models_used", []),
            "processing_time_seconds": ai_model_info.get("processing_time_seconds"),
            "note_length": ai_model_info.get("note_length"),
        }, user_id=user_id)

        # ── Save/update known speakers ──
        if speakers_info and user_id:
            save_identified_speakers(user_id, analysis_id, speakers_info)

        if utterances:
            rows = [
                {"user_id": user_id, "analysis_id": analysis_id,
                 "diarization_label": u["speaker"],
                 "start_seconds": u["start"], "end_seconds": u["end"],
                 "text": u["text"]}
                for u in utterances
            ]
            for i in range(0, len(rows), 500):
                sb_insert("speaker_utterances", rows[i:i+500])

        # ── Extract facts, entities, quotes, timeline, contradictions (async) ──
        import threading as _th
        def _post_pipeline():
            _src = (raw_data or {}).get("source", "unknown")
            extract_facts_and_entities(analysis_id, user_id, title, polished, description, source_type=_src)
            extract_quotes_and_timeline(analysis_id, user_id, title, polished, description)
            detect_contradictions(analysis_id, user_id, title, polished)
            log_custody(analysis_id, "enrichment_complete", {
                "enrichments": ["facts", "entities", "legal_entities", "quotes", "timeline", "contradictions"],
            }, user_id=user_id)
            print(f"[worker] Post-pipeline enrichment complete for {analysis_id}")
        _th.Thread(target=_post_pipeline, daemon=True).start()

        total = time.time() - t_start
        print(f"[worker] ✅ Complete in {total:.1f}s ({used_source}) for {analysis_id}")

    except Exception as e:
        fail_analysis(analysis_id, str(e))

# ── Speaker persistence ───────────────────────────────────────────────────────
def save_identified_speakers(user_id, analysis_id, speakers_info):
    """Save newly identified speakers to the speakers table."""
    try:
        existing = get_known_speakers(user_id)
        existing_names = {s["name"].lower() for s in existing}
        for sp in speakers_info:
            name = sp.get("likely_name") or sp.get("label", "Unknown")
            if name.lower() in existing_names or name.lower().startswith("speaker "):
                continue
            sb_insert("speakers", [{
                "user_id": user_id,
                "name": name,
                "channel": sp.get("role", ""),
                "notes": f"First seen in analysis {analysis_id}. {sp.get('role', '')}",
            }])
            existing_names.add(name.lower())
            print(f"[worker] New speaker saved: {name}")
    except Exception as e:
        print(f"[worker] Speaker save error: {e}")

# ── Fact Extraction ───────────────────────────────────────────────────────────
def extract_facts_and_entities(analysis_id, user_id, title, transcript, description="", source_type=None):
    """Extract factual claims and entities from a completed analysis — runs after main pipeline."""
    _source_type = source_type  # capture for closure in fact row building
    ctx = f'Video title: "{title}"\n' if title else ""
    if description:
        ctx += f'Description: "{description[:500]}"\n'
    t = transcript[:60000]

    prompts = [
        # Facts extraction
        (f'''Extract ALL factual claims from this transcript that would be useful for a book about American injustice.
Return ONLY valid JSON (no markdown): {{"facts": [
  {{"claim": "factual statement", "category": "legal|date|person|location|event|claim|quote", "confidence": "high|medium|low", "timestamp_hint": "approximate time context or null", "citation": "formatted citation"}}
]}}

Categories:
- legal: court cases, filings, legal proceedings, rights violations, statutes
- date: specific dates, time periods, deadlines mentioned
- person: people identified with roles/actions
- location: places, courts, jurisdictions, addresses
- event: specific events, incidents, meetings
- claim: allegations, assertions, accusations
- quote: direct quotes from speakers

For citations, format as: "[Speaker/Source], [Video Title], [approx timestamp if known]"

Be thorough — extract every verifiable fact. A book researcher needs these.''',
         f"{ctx}Transcript:\n{t}"),

        # Entity extraction
        (f'''Identify ALL people, organizations, courts, agencies, and places mentioned in this transcript.
Return ONLY valid JSON (no markdown): {{"entities": [
  {{"name": "full name", "type": "person|organization|court|agency|place|event", "aliases": ["alternate names"], "description": "brief description/role", "context_snippet": "short quote showing mention", "mention_count": 1}}
]}}

Be thorough — include every named entity, even if mentioned briefly. Include:
- All people (full names when possible)
- All organizations, agencies, departments
- All courts, jurisdictions
- All places, cities, counties, states
- Significant events referenced''',
         f"{ctx}Transcript:\n{t}"),
    ]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(call_openai, p[0], p[1], 8000) for p in prompts]
            facts_raw, entities_raw = [f.result() for f in futures]

        # Parse and save facts
        facts = []
        try:
            m = re.search(r"\{[\s\S]*\}", facts_raw)
            if m:
                facts = json.loads(m.group()).get("facts", [])
        except Exception as e:
            print(f"[worker] Facts parse error: {e}")

        if facts:
            fact_rows = []
            for f in facts:
                ts_hint = f.get("timestamp_hint")
                ts_seconds = None
                if ts_hint and isinstance(ts_hint, str):
                    # Try to parse "1:23" or "83s" style timestamps
                    ts_m = re.match(r'(\d+):(\d+)', ts_hint)
                    if ts_m:
                        ts_seconds = int(ts_m.group(1)) * 60 + int(ts_m.group(2))

                # Map text confidence to numeric score
                conf_text = f.get("confidence", "medium")
                conf_score = {"high": 0.85, "medium": 0.55, "low": 0.25}.get(conf_text, 0.5)
                # Refine with compute_confidence_score
                conf_score = compute_confidence_score(
                    f.get("claim", ""),
                    source_type=_source_type or "unknown",
                    speaker_identified=bool(f.get("citation")),
                )

                fact_rows.append({
                    "user_id": user_id,
                    "analysis_id": analysis_id,
                    "claim": f.get("claim", "")[:2000],
                    "category": f.get("category", "general"),
                    "source_timestamp": ts_seconds,
                    "citation": f.get("citation", ""),
                    "confidence": conf_text,
                    "confidence_score": conf_score,
                })
            for i in range(0, len(fact_rows), 50):
                sb_insert("facts", fact_rows[i:i+50])
            print(f"[worker] Saved {len(fact_rows)} facts for {analysis_id}")

        # Parse and save entities
        raw_entities = []
        try:
            m = re.search(r"\{[\s\S]*\}", entities_raw)
            if m:
                raw_entities = json.loads(m.group()).get("entities", [])
        except Exception as e:
            print(f"[worker] Entities parse error: {e}")

        if raw_entities:
            save_entities(user_id, analysis_id, raw_entities)

        # Legal precision: extract legal entities via regex
        legal_ents = extract_legal_entities_regex(transcript)
        if legal_ents:
            save_legal_entities(user_id, analysis_id, legal_ents)

    except Exception as e:
        print(f"[worker] Fact/entity extraction error: {e}")


def save_entities(user_id, analysis_id, raw_entities):
    """Save entities, merging with existing ones."""
    try:
        existing = sb_get("entities", {"user_id": user_id}, "id,name,entity_type,aliases")
        existing_map = {}
        for e in existing:
            existing_map[e["name"].lower()] = e
            for alias in (e.get("aliases") or []):
                existing_map[alias.lower()] = e

        for ent in raw_entities:
            name = ent.get("name", "").strip()
            if not name or len(name) < 2:
                continue
            ent_type = ent.get("type", "person")
            aliases = ent.get("aliases", [])

            # Check if entity already exists
            match = existing_map.get(name.lower())
            if not match:
                for alias in aliases:
                    match = existing_map.get(alias.lower())
                    if match:
                        break

            if match:
                # Add mention to existing entity
                sb_insert("entity_mentions", [{
                    "entity_id": match["id"],
                    "analysis_id": analysis_id,
                    "context": (ent.get("context_snippet") or "")[:500],
                    "role": (ent.get("description") or "")[:500],
                    "mention_count": ent.get("mention_count", 1),
                }])
            else:
                # Create new entity
                url = f"{SUPABASE_URL}/rest/v1/entities?select=id"
                headers = {**sb_headers(), "Prefer": "return=representation"}
                row = {
                    "user_id": user_id,
                    "name": name,
                    "entity_type": ent_type,
                    "aliases": aliases,
                    "description": (ent.get("description") or "")[:1000],
                    "first_seen_analysis": analysis_id,
                }
                req = Request(url, data=json.dumps(row).encode(), headers=headers, method="POST")
                try:
                    resp = json.loads(urlopen(req).read())
                    if resp and isinstance(resp, list) and resp[0].get("id"):
                        entity_id = resp[0]["id"]
                        existing_map[name.lower()] = {"id": entity_id, "name": name}
                        # Add first mention
                        sb_insert("entity_mentions", [{
                            "entity_id": entity_id,
                            "analysis_id": analysis_id,
                            "context": (ent.get("context_snippet") or "")[:500],
                            "role": (ent.get("description") or "")[:500],
                            "mention_count": ent.get("mention_count", 1),
                        }])
                except HTTPError as e:
                    print(f"[worker] Entity insert error: {e.status}: {e.read()}")

        print(f"[worker] Processed {len(raw_entities)} entities for {analysis_id}")
    except Exception as e:
        print(f"[worker] Entity save error: {e}")


# ── Quote & Timeline Extraction ───────────────────────────────────────────────
def extract_quotes_and_timeline(analysis_id, user_id, title, transcript, description=""):
    """Extract notable quotes and timeline events from a completed analysis."""
    ctx = f'Video title: "{title}"\n' if title else ""
    if description:
        ctx += f'Description: "{description[:500]}"\n'
    t = transcript[:60000]

    prompts = [
        # Quote extraction
        (f'''Extract ALL notable direct quotes from this transcript. Focus on:
- Statements that could be evidence in a legal/injustice context
- Admissions, denials, threats, promises
- Key testimony or witness statements
- Powerful/emotional statements
- Anything quotable for a book

Return ONLY valid JSON (no markdown): {{"quotes": [
  {{"speaker": "speaker name or identifier", "quote_text": "exact or near-exact quote", "context": "what was happening when this was said", "timestamp_hint": "approximate time or null", "significance": "high|medium|low", "tags": ["evidence", "testimony", "threat", "admission", "denial", "emotional", "legal"]}}
]}}

Be thorough — a book author needs every usable quote.''',
         f"{ctx}Transcript:\n{t}"),

        # Timeline extraction
        (f'''Extract ALL dates, time references, and chronological events mentioned in this transcript.
Include:
- Specific dates mentioned (filing dates, incident dates, meeting dates)
- Relative time references ("last week", "three months ago") — estimate the actual date if possible
- Sequence of events described
- Deadlines mentioned

Return ONLY valid JSON (no markdown): {{"events": [
  {{"event_date": "YYYY-MM-DD or YYYY-MM or YYYY (best estimate)", "precision": "exact|month|year|estimated", "description": "what happened", "source_context": "the quote or context that mentions this date", "category": "filing|hearing|incident|deadline|arrest|ruling|other", "confidence": "high|medium|low"}}
]}}

Use context clues to estimate dates. If the video was likely recorded around a certain date, use that to resolve relative references.''',
         f"{ctx}Transcript:\n{t}"),
    ]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(call_openai, p[0], p[1], 8000) for p in prompts]
            quotes_raw, timeline_raw = [f.result() for f in futures]

        # Parse and save quotes
        quotes = []
        try:
            m = re.search(r"\{[\s\S]*\}", quotes_raw)
            if m:
                quotes = json.loads(m.group()).get("quotes", [])
        except Exception as e:
            print(f"[worker] Quotes parse error: {e}")

        if quotes:
            rows = []
            for q in quotes:
                ts = None
                hint = q.get("timestamp_hint")
                if hint and isinstance(hint, str):
                    ts_m = re.match(r'(\d+):(\d+)', hint)
                    if ts_m:
                        ts = int(ts_m.group(1)) * 60 + int(ts_m.group(2))
                rows.append({
                    "user_id": user_id,
                    "analysis_id": analysis_id,
                    "speaker": (q.get("speaker") or "Unknown")[:200],
                    "quote_text": (q.get("quote_text") or "")[:2000],
                    "context": (q.get("context") or "")[:500],
                    "timestamp_seconds": ts,
                    "significance": q.get("significance", "medium"),
                    "tags": q.get("tags", []),
                })
            for i in range(0, len(rows), 50):
                sb_insert("quotes", rows[i:i+50])
            print(f"[worker] Saved {len(rows)} quotes for {analysis_id}")

        # Parse and save timeline events
        events = []
        try:
            m = re.search(r"\{[\s\S]*\}", timeline_raw)
            if m:
                events = json.loads(m.group()).get("events", [])
        except Exception as e:
            print(f"[worker] Timeline parse error: {e}")

        if events:
            rows = []
            for ev in events:
                rows.append({
                    "user_id": user_id,
                    "analysis_id": analysis_id,
                    "event_date": (ev.get("event_date") or "unknown")[:50],
                    "event_date_precision": ev.get("precision", "estimated"),
                    "event_description": (ev.get("description") or "")[:1000],
                    "source_context": (ev.get("source_context") or "")[:500],
                    "category": ev.get("category", "event"),
                    "confidence": ev.get("confidence", "medium"),
                })
            for i in range(0, len(rows), 50):
                sb_insert("timeline_events", rows[i:i+50])
            print(f"[worker] Saved {len(rows)} timeline events for {analysis_id}")

    except Exception as e:
        print(f"[worker] Quote/timeline extraction error: {e}")


def detect_contradictions(analysis_id, user_id, title, transcript):
    """Compare new analysis against existing facts to find contradictions."""
    try:
        # Get existing facts for this user
        url = f"{SUPABASE_URL}/rest/v1/facts?user_id=eq.{user_id}&select=claim,category,analysis_id,citation&limit=200"
        req = Request(url, headers={**sb_headers(), "Prefer": ""})
        existing_facts = json.loads(urlopen(req).read())

        # Filter out facts from current analysis
        other_facts = [f for f in existing_facts if f["analysis_id"] != analysis_id]
        if not other_facts:
            print(f"[worker] No prior facts to compare for contradiction detection")
            return

        # Build fact summary for comparison
        fact_lines = []
        for f in other_facts[:100]:
            fact_lines.append(f"[{f['analysis_id'][:8]}] ({f.get('category','')}) {f['claim']}")
        fact_block = "\n".join(fact_lines)

        prompt = f'''Compare the claims in this NEW video against EXISTING facts from other videos by the same user.
Identify any contradictions, inconsistencies, or conflicting accounts.

EXISTING FACTS:
{fact_block[:20000]}

NEW VIDEO: "{title}"
{transcript[:30000]}

Return ONLY valid JSON (no markdown): {{"contradictions": [
  {{"claim_a": "the existing fact that conflicts", "claim_a_source_id": "the 8-char analysis ID prefix from brackets", "claim_b": "the contradicting claim from this new video", "explanation": "why these conflict", "severity": "high|medium|low"}}
]}}

If no contradictions found, return {{"contradictions": []}}.
Only flag genuine contradictions or inconsistencies, not minor differences in wording.'''

        response = call_openai(prompt, "", 3000)
        contradictions = []
        try:
            m = re.search(r"\{[\s\S]*\}", response)
            if m:
                contradictions = json.loads(m.group()).get("contradictions", [])
        except Exception as e:
            print(f"[worker] Contradictions parse error: {e}")

        if contradictions:
            rows = []
            # Build lookup for full analysis IDs
            id_prefix_map = {}
            for f in other_facts:
                id_prefix_map[f["analysis_id"][:8]] = f["analysis_id"]

            for c in contradictions:
                src_prefix = c.get("claim_a_source_id", "")
                claim_a_id = id_prefix_map.get(src_prefix, other_facts[0]["analysis_id"] if other_facts else analysis_id)
                rows.append({
                    "user_id": user_id,
                    "claim_a_analysis_id": claim_a_id,
                    "claim_b_analysis_id": analysis_id,
                    "claim_a": (c.get("claim_a") or "")[:2000],
                    "claim_b": (c.get("claim_b") or "")[:2000],
                    "explanation": (c.get("explanation") or "")[:1000],
                    "severity": c.get("severity", "medium"),
                })
            if rows:
                sb_insert("contradictions", rows)
                print(f"[worker] Found {len(rows)} contradictions for {analysis_id}")
        else:
            print(f"[worker] No contradictions found for {analysis_id}")

    except Exception as e:
        print(f"[worker] Contradiction detection error: {e}")


# ── Cross-Video Search ────────────────────────────────────────────────────────
def handle_search(data):
    """Search across all analyses for a user."""
    query = data.get("query", "").strip()
    user_id = data.get("user_id")
    if not query or not user_id:
        return {"error": "query and user_id required"}

    results = []
    query_lower = query.lower()
    terms = query_lower.split()

    # Fetch all analyses for this user
    url = f"{SUPABASE_URL}/rest/v1/analyses?user_id=eq.{user_id}&select=id,title,youtube_id,channel,summary,polished_transcript,expanded_notes,likely_production_date,raw_transcript&order=created_at.desc"
    headers = {**sb_headers(), "Prefer": ""}
    req = Request(url, headers=headers)
    try:
        analyses = json.loads(urlopen(req).read())
    except Exception as e:
        return {"error": f"DB error: {e}"}

    for a in analyses:
        # Search across multiple fields
        fields = {
            "summary": a.get("summary") or "",
            "transcript": a.get("polished_transcript") or "",
            "notes": a.get("expanded_notes") or "",
            "date": f"{a.get('likely_production_date', '')} {(a.get('raw_transcript') or {}).get('production_date_reasoning', '')}",
        }

        for field_name, text in fields.items():
            if not text:
                continue
            text_lower = text.lower()
            # Check if all search terms appear
            if all(term in text_lower for term in terms):
                # Extract snippet around first match
                idx = text_lower.find(terms[0])
                start = max(0, idx - 80)
                end = min(len(text), idx + len(terms[0]) + 150)
                snippet = text[start:end].strip()
                if start > 0:
                    snippet = "…" + snippet
                if end < len(text):
                    snippet = snippet + "…"

                results.append({
                    "analysis_id": a["id"],
                    "title": a.get("title"),
                    "youtube_id": a.get("youtube_id"),
                    "channel": a.get("channel"),
                    "snippet": snippet,
                    "field": field_name,
                    "likely_production_date": a.get("likely_production_date"),
                })
                break  # One result per analysis

    # Also search facts
    fact_url = f"{SUPABASE_URL}/rest/v1/facts?user_id=eq.{user_id}&select=claim,category,analysis_id,citation"
    req = Request(fact_url, headers={**sb_headers(), "Prefer": ""})
    try:
        facts = json.loads(urlopen(req).read())
        for f in facts:
            claim_lower = (f.get("claim") or "").lower()
            if all(term in claim_lower for term in terms):
                # Check if we already have this analysis in results
                existing_ids = {r["analysis_id"] for r in results}
                if f["analysis_id"] not in existing_ids:
                    results.append({
                        "analysis_id": f["analysis_id"],
                        "title": None,  # Will be populated by frontend join
                        "youtube_id": None,
                        "channel": None,
                        "snippet": f["claim"][:200],
                        "field": f"fact ({f.get('category', 'general')})",
                        "likely_production_date": None,
                    })
    except Exception:
        pass

    return {"results": results[:50]}


# ── AI Chat endpoint ──────────────────────────────────────────────────────────
def handle_chat(data):
    """Process an AI chat message about a video analysis."""
    analysis_id = data.get("analysis_id")
    message = data.get("message", "")
    user_id = data.get("user_id")

    if not analysis_id or not message:
        return {"error": "analysis_id and message are required"}

    # Load analysis data
    rows = sb_get("analyses", {"id": analysis_id})
    if not rows:
        return {"error": "Analysis not found"}
    analysis = rows[0]

    # Load utterances
    utts = sb_get("speaker_utterances", {"analysis_id": analysis_id}, "text,diarization_label,start_seconds")

    # Build context
    context = f"""Video: "{analysis.get('title', 'Unknown')}"
Channel: {analysis.get('channel', 'Unknown')}
Likely recorded: {analysis.get('likely_production_date', 'Unknown')}
Date reasoning: {analysis.get('production_date_reasoning', '')}

Summary: {analysis.get('summary', '')}

Polished Transcript:
{(analysis.get('polished_transcript') or '')[:30000]}

Notes:
{(analysis.get('expanded_notes') or '')[:4000]}
"""

    instructions = f"""You are TubeScribe AI assistant. The user is chatting about a specific video analysis.
You have access to the video's transcript, summary, notes, date info, and speaker data.

When the user asks you to UPDATE something (date, speaker name, notes, transcript detail), you should:
1. Make the change
2. Return your response with a JSON block at the end like:
   ```json
   {{"updates": {{"field_name": "new_value"}}}}
   ```
   Valid fields: likely_production_date, production_date_reasoning, expanded_notes, polished_transcript, summary

If the user is just asking a question (not requesting changes), just answer naturally without the JSON block.

Be helpful, conversational, and accurate. Reference specific parts of the transcript when relevant."""

    try:
        response = call_openai(instructions, f"Video context:\n{context}\n\nUser message: {message}", max_tokens=3000)

        # Check for updates in the response
        update_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', response)
        updates_applied = {}
        if update_match:
            try:
                update_data = json.loads(update_match.group(1))
                updates = update_data.get("updates", {})
                valid_fields = {"likely_production_date", "production_date_reasoning", "expanded_notes", "polished_transcript", "summary"}
                clean_updates = {k: v for k, v in updates.items() if k in valid_fields}
                if clean_updates:
                    sb_patch("analyses", {"id": analysis_id}, clean_updates)
                    updates_applied = clean_updates
                    print(f"[chat] Updated fields: {list(clean_updates.keys())} for {analysis_id}")
            except Exception as e:
                print(f"[chat] Update parse error: {e}")

            # Clean the JSON block from the user-facing response
            clean_response = response[:update_match.start()].strip()
            if not clean_response:
                clean_response = "Done! I've updated that for you."
        else:
            clean_response = response

        return {
            "response": clean_response,
            "updates_applied": updates_applied,
        }
    except Exception as e:
        return {"error": str(e)}

# ── HTTP Server ───────────────────────────────────────────────────────────────

# ── Stripe: checkout / confirm / webhook ────────────────────────────────────
def stripe_request(method, path, params=None):
    body = urlencode(params).encode() if params else b""
    req = Request(f"https://api.stripe.com{path}", data=body if method == "POST" else None,
                  method=method, headers={
                      "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
                      "Content-Type": "application/x-www-form-urlencoded"})
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read())

def handle_stripe_checkout(payload):
    if not STRIPE_SECRET_KEY:
        return {"error": "Stripe not configured (STRIPE_SECRET_KEY not set on the worker)"}
    plan = (payload.get("plan") or "monthly").lower()
    user_id = payload.get("userId") or payload.get("user_id")
    origin = payload.get("origin")
    if not user_id or not origin:
        return {"error": "userId and origin required"}
    prices = {"monthly":  (STRIPE_PRICE_MONTHLY,  "subscription"),
              "yearly":   (STRIPE_PRICE_YEARLY,   "subscription"),
              "lifetime": (STRIPE_PRICE_LIFETIME, "payment")}
    if plan not in prices:
        return {"error": f"Unknown plan '{plan}' (use monthly | yearly | lifetime)"}
    price_id, mode = prices[plan]
    params = {"mode": mode,
              "line_items[0][price]": price_id,
              "line_items[0][quantity]": "1",
              "success_url": f"{origin}/dashboard?upgrade=success&plan={plan}&session_id={{CHECKOUT_SESSION_ID}}",
              "cancel_url": f"{origin}/dashboard?upgrade=cancelled",
              "client_reference_id": user_id,
              "metadata[user_id]": user_id,
              "metadata[plan]": plan,
              "allow_promotion_codes": "true"}
    if mode == "subscription":
        params["subscription_data[metadata][user_id]"] = user_id
        params["subscription_data[metadata][plan]"] = plan
    session = stripe_request("POST", "/v1/checkout/sessions", params)
    return {"url": session.get("url"), "plan": plan}

def handle_stripe_confirm(session_id):
    if not STRIPE_SECRET_KEY:
        return {"paid": False, "error": "Stripe not configured"}
    session = stripe_request("GET", f"/v1/checkout/sessions/{session_id}")
    paid = session.get("payment_status") == "paid"
    return {"paid": paid,
            "userId": session.get("client_reference_id") or (session.get("metadata") or {}).get("user_id")}

def verify_stripe_signature(payload_bytes, sig_header):
    if not STRIPE_WEBHOOK_SECRET:
        return False
    try:
        parts = dict(p.split("=", 1) for p in (sig_header or "").split(",") if "=" in p)
        t, v1 = parts.get("t"), parts.get("v1")
        if not t or not v1:
            return False
        expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), f"{t}.".encode() + payload_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)
    except Exception:
        return False

def handle_stripe_webhook(payload_bytes, sig_header):
    if not STRIPE_WEBHOOK_SECRET:
        return {"received": False, "error": "STRIPE_WEBHOOK_SECRET not set"}
    if not verify_stripe_signature(payload_bytes, sig_header):
        return {"received": False, "error": "invalid signature"}
    event = json.loads(payload_bytes.decode() or "{}")
    etype = event.get("type", "")
    if etype == "checkout.session.completed":
        sess = (event.get("data") or {}).get("object") or {}
        uid = sess.get("client_reference_id") or (sess.get("metadata") or {}).get("user_id")
        if uid:
            try:
                sb_patch("profiles", {"id": uid}, {"is_unlimited": True})
                print(f"[stripe] checkout completed — is_unlimited granted to {uid}")
            except Exception as e:
                print(f"[stripe] profile patch failed: {e}")
    elif etype == "customer.subscription.deleted":
        sub = (event.get("data") or {}).get("object") or {}
        uid = (sub.get("metadata") or {}).get("user_id")
        plan = (sub.get("metadata") or {}).get("plan")
        if uid and plan != "lifetime":
            try:
                sb_patch("profiles", {"id": uid}, {"is_unlimited": False})
                print(f"[stripe] subscription cancelled — is_unlimited revoked for {uid}")
            except Exception as e:
                print(f"[stripe] downgrade patch failed: {e}")
    elif etype == "invoice.payment_failed":
        inv = (event.get("data") or {}).get("object") or {}
        print(f"[stripe] payment failed (invoice {inv.get('id')})")
    return {"received": True, "type": etype}

# ── Admin /status diagnostics (service-role bearer required) ────────────────
def _sb_rest(path, method="GET", data=None, prefer=None):
    headers = {"apikey": SUPABASE_SERVICE_ROLE, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
               "Content-Type": "application/json"}
    if prefer: headers["Prefer"] = prefer
    req = Request(f"{SUPABASE_URL}/rest/v1/{path}", data=json.dumps(data).encode() if data else None,
                  method=method, headers=headers)
    return json.loads(urlopen(req, timeout=30).read() or b"null")

def handle_status(bearer):
    if (bearer or "") != SUPABASE_SERVICE_ROLE:
        return None
    out = {"version": WORKER_VERSION, "stripe": bool(STRIPE_SECRET_KEY),
           "supadata_keys": len(SUPADATA_KEYS), "poller": "running"}
    try:
        rows = _sb_rest("analyses?select=status&order=created_at.desc&limit=2000")
        counts = {}
        for r in rows:
            s = r.get("status") or "unknown"
            counts[s] = counts.get(s, 0) + 1
        out["db"] = "ok"
        out["recent_status_counts"] = counts
    except Exception as e:
        out["db"] = f"error: {str(e)[:150]}"
        return out
    try:
        out["recent_failures"] = _sb_rest("analyses?status=eq.failed&select=id,error_message,created_at&order=created_at.desc&limit=5")
    except Exception:
        out["recent_failures"] = []
    try:
        pend = _sb_rest("analyses?status=eq.pending&select=id,created_at&order=created_at.asc&limit=1")
        out["oldest_pending"] = pend[0] if pend else None
    except Exception:
        out["oldest_pending"] = None
    return out

# ── Pending-analysis poller (webhook-free trigger) ──────────────────────────
def pending_poller(interval=20):
    print(f"[poller] watching for pending analyses every {interval}s")
    while True:
        try:
            rows = _sb_rest("analyses?status=eq.pending&order=created_at.asc&limit=3&select=*")
            for record in rows or []:
                claimed = _sb_rest(f"analyses?id=eq.{record['id']}&status=eq.pending", method="PATCH",
                                   data={"status": "processing"}, prefer="return=representation")
                if claimed:
                    print(f"[poller] claimed {record['id']} — starting pipeline")
                    threading.Thread(target=run_pipeline, args=(record,), daemon=True).start()
        except Exception as e:
            print(f"[poller] error: {str(e)[:150]}")
        time.sleep(interval)

class Handler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip("/")
        # ── Test AI endpoint ──
        if path == "/test-ai":
            result = {"groq_key_set": bool(GROQ_API_KEY), "groq_key_prefix": GROQ_API_KEY[:8] + "..." if GROQ_API_KEY else "none"}
            try:
                out = _call_api_once("You are a helpful assistant.", "Say 'AI working' in exactly two words.", 20, model=GROQ_MODEL, provider="groq")
                result["primary"] = {"status": "ok", "model": GROQ_MODEL, "response": out[:100]}
            except Exception as e:
                result["primary"] = {"status": "error", "model": GROQ_MODEL, "error": str(e)[:300]}
            try:
                out2 = _call_api_once("You are a helpful assistant.", "Say 'AI working' in exactly two words.", 20, model=GROQ_FALLBACK, provider="groq")
                result["fallback"] = {"status": "ok", "model": GROQ_FALLBACK, "response": out2[:100]}
            except Exception as e:
                result["fallback"] = {"status": "error", "model": GROQ_FALLBACK, "error": str(e)[:300]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
            return
        # ── Stress-test AI: 6 parallel calls like real processing ──
        if path == "/test-ai-parallel":
            fake_transcript = "This is a test transcript. " * 200  # ~1000 words
            results = {}
            def _test_call(label):
                try:
                    out = call_openai("Summarize in one sentence.", fake_transcript, 100)
                    return {"status": "ok", "response": out[:100]}
                except Exception as e:
                    return {"status": "error", "error": str(e)[:300]}
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(_test_call, f"call_{i}"): f"call_{i}" for i in range(6)}
                for f in futs:
                    label = futs[f]
                    results[label] = f.result()
            ok_count = sum(1 for r in results.values() if r["status"] == "ok")
            results["summary"] = f"{ok_count}/6 succeeded"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(results, indent=2).encode())
            return
        # ── Supadata key-pool health (live snapshot for APEX monitoring) ──
        if path == "/supadata-status":
            result = handle_supadata_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
            return
        # ── Stripe: verify a completed checkout session ──
        if path == "/stripe/confirm":
            from urllib.parse import parse_qs, urlparse
            session_id = (parse_qs(urlparse(self.path).query).get("session_id") or [""])[0]
            try:
                result = handle_stripe_confirm(session_id)
            except Exception as e:
                result = {"paid": False, "error": str(e)[:200]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        # ── Admin status diagnostics (service-role bearer required) ──
        if path == "/status":
            result = handle_status(self.headers.get("Authorization", "").replace("Bearer ", ""))
            code = 200 if result else 401
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            if result:
                self.wfile.write(json.dumps(result, indent=2).encode())
            else:
                self.wfile.write(b'{"error":"unauthorized"}')
            return
        supadata = f"{len(SUPADATA_KEYS)} keys" if SUPADATA_KEYS else "no"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        # Report the full provider chain in quality-priority order
        _chain_parts = []
        if USE_MISTRAL:  _chain_parts.append(f"mistral ({MISTRAL_MODEL})")
        if USE_CEREBRAS: _chain_parts.append(f"cerebras ({CEREBRAS_MODEL})")
        if USE_OPENROUTER: _chain_parts.append(f"openrouter ({OPENROUTER_MODEL})")
        if USE_GROQ:     _chain_parts.append(f"groq ({GROQ_MODEL}/{GROQ_FALLBACK})")
        ai_chain = " → ".join(_chain_parts) if _chain_parts else "none"
        ai_primary = _chain_parts[0] if _chain_parts else "none"
        ai_fallback = " → ".join(_chain_parts[1:]) if len(_chain_parts) > 1 else "none"
        providers_detail = []
        if USE_MISTRAL:
            providers_detail.append({"name": "Mistral", "model": MISTRAL_MODEL, "configured": True})
        else:
            providers_detail.append({"name": "Mistral", "configured": False, "hint": "Set MISTRAL_API_KEY (console.mistral.ai)"})
        if USE_CEREBRAS:
            providers_detail.append({"name": "Cerebras", "model": CEREBRAS_MODEL, "configured": True})
        else:
            providers_detail.append({"name": "Cerebras", "configured": False, "hint": "gpt-oss-120b — set CEREBRAS_API_KEY (cerebras.ai)"})
        if USE_OPENROUTER:
            providers_detail.append({"name": "OpenRouter", "model": OPENROUTER_MODEL, "configured": True})
        else:
            providers_detail.append({"name": "OpenRouter", "configured": False, "hint": "Free models — set OPENROUTER_API_KEY"})
        if USE_GROQ:
            providers_detail.append({"name": "Groq", "model": GROQ_MODEL, "configured": True})
        else:
            providers_detail.append({"name": "Groq", "configured": False, "hint": "Free 100K tokens/day — set GROQ_API_KEY"})
        deepgram_ok = bool(DEEPGRAM_API_KEY)
        estimated_tokens_per_analysis = "~55K tokens (with optimized limits)"
        self.wfile.write(json.dumps({
            "status": "ok", "version": WORKER_VERSION, "supadata": supadata, "forensic": True,
            "ai_provider": AI_PROVIDER,
            "ai_primary": ai_primary,
            "ai_fallback": ai_fallback,
            "ai_chain": ai_chain,
            "deepgram": deepgram_ok,
            "providers": providers_detail,
            "estimated_tokens_per_analysis": estimated_tokens_per_analysis,
        }, indent=2).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self._cors_headers()
            self.end_headers()
            return

        path = self.path.rstrip("/")

        # ── Chat endpoint ──
        if path == "/chat":
            result = handle_chat(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
# ── Stripe: create checkout session ──
        if path == "/stripe/checkout-session":
            result = handle_stripe_checkout(payload)
            if result.get("error"):
                code = 400 if "required" in result["error"] or "Unknown plan" in result["error"] else 503
                self.send_response(code)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        # ── Stripe: webhook events (signature-verified) ──
        if path == "/stripe/webhook":
            sig = self.headers.get("Stripe-Signature", "")
            result = handle_stripe_webhook(body, sig)
            self.send_response(200 if result.get("received") else (400 if "signature" in result.get("error", "") else 503))
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
            return

        # ── Search endpoint ──
        if path == "/search":
            result = handle_search(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # ── Reprocess insights endpoint ──
        if path == "/reprocess-insights":
            import threading
            def _reprocess():
                result = handle_reprocess_insights(payload)
                print(f"[reprocess] Result: {json.dumps(result)[:200]}")
            threading.Thread(target=_reprocess, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "processing": payload.get("analysis_id")}).encode())
            return

        # ── Export endpoint ──
        if path == "/export":
            result = handle_export(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # ── Forensic export endpoint ──
        if path == "/export-forensic":
            result = handle_forensic_export(payload)
            log_custody(payload.get("analysis_id"), "exported", {
                "format": "forensic_certificate",
            }, user_id=payload.get("user_id"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # ── Hash verification endpoint ──
        if path == "/verify-hash":
            result = handle_verify_hash(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # ── Custody log endpoint ──
        if path == "/custody-log":
            result = handle_custody_log(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # ── Default: webhook trigger ──
        record = payload.get("record", payload)
        if record.get("status", "pending") != "pending":
            self.send_response(200)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(b'{"ok":true,"skipped":true}')
            return

        import threading
        threading.Thread(target=run_pipeline, args=(record,), daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "id": record.get("id")}).encode())

    def log_message(self, format, *args):
        print(f"[http] {args[0]} {args[1]}")


def handle_reprocess_insights(data):
    """Re-run AI insights on an existing analysis using stored transcript data."""
    analysis_id = data.get("analysis_id")
    note_length = data.get("note_length", "medium")
    if not analysis_id:
        return {"error": "analysis_id required"}
    log_custody(analysis_id, "reprocessed", {"note_length": note_length}, user_id=data.get("user_id"))
    
    rows = sb_get("analyses", {"id": analysis_id}, "title,user_id,polished_transcript,expanded_notes,summary")
    if not rows:
        return {"error": "analysis not found"}
    
    row = rows[0]
    title = row.get("title", "")
    user_id = row.get("user_id")
    
    # Get transcript from speaker_utterances
    utts = sb_get("speaker_utterances", {"analysis_id": analysis_id}, "text")
    transcript = "\n".join(u["text"] for u in utts) if utts else ""
    
    if not transcript or len(transcript) < 50:
        # Fall back to polished_transcript
        pt = row.get("polished_transcript", "")
        if pt and len(pt) > 50:
            transcript = pt
    
    if not transcript or len(transcript) < 50:
        return {"error": f"no transcript data found ({len(transcript)} chars)"}
    
    print(f"[reprocess] Starting insights for {analysis_id[:8]} ({len(transcript)} chars) note_length={note_length}")
    set_status(analysis_id, "processing")
    
    try:
        insights = generate_insights(transcript, title, user_id=user_id, note_length=note_length)
        speakers_info = insights.pop("speakers_info", [])
        ai_model_info = insights.pop("ai_model_info", {})
        ai_provenance = insights.pop("ai_provenance", {})
        
        # Merge model info into existing raw_transcript JSON
        raw_rows = sb_get("analyses", {"id": analysis_id}, "raw_transcript")
        existing_raw = (raw_rows[0].get("raw_transcript") or {}) if raw_rows else {}
        existing_raw["ai_model_info"] = ai_model_info
        
        sb_patch("analyses", {"id": analysis_id}, {
            "status": "complete",
            "error_message": None,
            "raw_transcript": existing_raw,
            "ai_provenance": ai_provenance,
            **insights,
        })
        
        if speakers_info and user_id:
            save_identified_speakers(user_id, analysis_id, speakers_info)
        
        print(f"[reprocess] Completed {analysis_id[:8]} — model: {ai_model_info.get('primary_model', 'unknown')}")
        return {"ok": True, "id": analysis_id, "ai_model_info": ai_model_info}
    except Exception as e:
        fail_analysis(analysis_id, f"Reprocess error: {str(e)}")
        return {"error": str(e)}

def handle_export(data):
    """Generate a comprehensive export of the analysis."""
    analysis_id = data.get("analysis_id")
    if not analysis_id:
        return {"error": "analysis_id required"}
    rows = sb_get("analyses", {"id": analysis_id})
    if not rows:
        return {"error": "Analysis not found"}
    a = rows[0]
    log_custody(analysis_id, "exported", {"format": "standard"}, user_id=data.get("user_id"))
    utts = sb_get("speaker_utterances", {"analysis_id": analysis_id}, "text,diarization_label,start_seconds,end_seconds")

    speakers_info = []
    raw = a.get("raw_transcript")
    if isinstance(raw, dict):
        speakers_info = raw.get("speakers_info", [])

    sections = []
    sections.append(a.get('title', 'Untitled Video').upper())
    sections.append("=" * len(a.get('title', 'Untitled Video')))
    sections.append(f"Channel: {a.get('channel', 'Unknown')}")
    sections.append(f"URL: {a.get('youtube_url', '')}")
    if a.get("likely_production_date"):
        sections.append(f"Likely Recorded: {a['likely_production_date']}")
        if a.get("production_date_reasoning"):
            sections.append(f"Date Evidence: {a['production_date_reasoning']}")
    sections.append(f"Analyzed: {a.get('created_at', '')[:10]}")
    sections.append("")

    # Summary
    sections.append("SUMMARY")
    sections.append("-" * 40)
    sections.append(sanitize_for_legal(a.get("summary", "N/A")))
    sections.append("")

    # Sentiment
    sections.append("SENTIMENT ANALYSIS")
    sections.append("-" * 40)
    sent = a.get("sentiment", {})
    if isinstance(sent, dict):
        sections.append(f"Overall: {sent.get('overall', 'N/A')}")
        if sent.get("score") is not None:
            sections.append(f"Score: {sent['score']} (-1 to +1)")
        if sent.get("tone"):
            sections.append(f"Tone: {sent['tone']}")
        if sent.get("key_emotions"):
            sections.append(f"Key Emotions: {', '.join(sent['key_emotions'])}")
    sections.append("")

    # Speakers
    if speakers_info:
        sections.append("IDENTIFIED SPEAKERS")
        sections.append("-" * 40)
        for sp in speakers_info:
            name = sp.get("likely_name") or sp.get("label", "Unknown")
            role = sp.get("role", "")
            pct = sp.get("speaking_percentage", "")
            sections.append(f"  - {name}: {role} ({pct}% of dialogue)")
        sections.append("")

    # Polished Transcript
    sections.append("POLISHED TRANSCRIPT")
    sections.append("-" * 40)
    sections.append(sanitize_for_legal(a.get("polished_transcript", "N/A")))
    sections.append("")

    # Speaker Utterances (raw with timestamps)
    if utts:
        sections.append("RAW SPEAKER TRANSCRIPT (WITH TIMESTAMPS)")
        sections.append("-" * 40)
        for u in sorted(utts, key=lambda x: x.get("start_seconds") or 0):
            ts = ""
            if u.get("start_seconds") is not None:
                m = int(u["start_seconds"] // 60)
                s = int(u["start_seconds"] % 60)
                ts = f"[{m}:{s:02d}] "
            label = u.get("diarization_label", "Speaker")
            sections.append(f"{ts}{label}: {u['text']}")
        sections.append("")

    # Notes
    sections.append("EXPANDED NOTES")
    sections.append("-" * 40)
    sections.append(sanitize_for_legal(a.get("expanded_notes", "N/A")))

    return {"text": "\n".join(sections), "title": a.get("title", "export")}

if __name__ == "__main__":
    _parts = []
    if USE_MISTRAL:  _parts.append(f"Mistral({MISTRAL_MODEL})")
    if USE_CEREBRAS: _parts.append(f"Cerebras({CEREBRAS_MODEL})")
    if USE_OPENROUTER: _parts.append(f"OpenRouter({OPENROUTER_MODEL})")
    if USE_GROQ:     _parts.append(f"Groq({GROQ_MODEL})")
    ai_chain = " → ".join(_parts) if _parts else "none"
    print(f"[worker] {WORKER_VERSION} — Supadata: {len(SUPADATA_KEYS)} key(s) | Forensic: enabled")
    print(f"[worker] AI chain: {ai_chain}")
    print(f"[worker] Listening on port {PORT}")
    threading.Thread(target=pending_poller, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
