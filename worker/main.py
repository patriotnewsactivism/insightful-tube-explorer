"""
TubeScribe: Audio Worker v14
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
v15: CaseBuddy integration bridge + MCP server (7 tools for AI agent consumption)
"""

import os, json, time, hmac, hashlib, base64, tempfile, subprocess, re, uuid, io, html as html_mod
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
# ── AI provider config (Cerebras preferred: 1M tokens/day free) ─────────────
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL   = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")
# ── OpenRouter (free tier: 27 free models, Google login) ───────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
# ── Groq (fallback — free tier: 100K tokens/day) ───────────────────────────
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL       = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACK    = os.environ.get("GROQ_FALLBACK_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
USE_GROQ         = bool(GROQ_API_KEY)
USE_CEREBRAS     = bool(CEREBRAS_API_KEY)
USE_OPENROUTER   = bool(OPENROUTER_API_KEY)
# Which provider to try first
AI_PROVIDER      = "cerebras" if USE_CEREBRAS else ("openrouter" if USE_OPENROUTER else "groq")
SUPADATA_API_KEY         = os.environ.get("SUPADATA_API_KEY", "")
DEEPGRAM_API_KEY         = os.environ.get("DEEPGRAM_API_KEY", "")
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

WORKER_VERSION = "v15"

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


# ── CaseBuddy Integration Bridge ────────────────────────────────────────────
def handle_casebuddy_export(data):
    """Package an analysis as a CaseBuddy-compatible evidence object.
    
    Returns a JSON payload matching CaseBuddy's Evidence + Case integration format:
    - evidence: Evidence object (id, name, type, description, dateObtained, etc.)
    - forensic: integrity data (hash, captured_at, chain of custody)
    - transcript: polished + raw transcripts
    - insights: summary, sentiment, quotes, timeline, contradictions, facts, entities
    - metadata: source URL, channel, speakers
    """
    analysis_id = data.get("analysis_id")
    if not analysis_id:
        return {"error": "analysis_id required"}

    headers = {"apikey": os.environ["SUPABASE_KEY"],
               "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
               "Content-Type": "application/json"}
    base = os.environ["SUPABASE_URL"]

    # Fetch the analysis
    url = f"{base}/rest/v1/analyses?id=eq.{analysis_id}&select=*"
    req = Request(url, headers=headers)
    resp = json.loads(urlopen(req).read())
    if not resp:
        return {"error": "Analysis not found"}
    a = resp[0]

    # Fetch enrichment tables
    enrichments = {}
    for table in ["facts", "entities", "quotes", "timeline_events", "contradictions", "chapters"]:
        try:
            turl = f"{base}/rest/v1/{table}?analysis_id=eq.{analysis_id}&select=*"
            treq = Request(turl, headers=headers)
            enrichments[table] = json.loads(urlopen(treq).read())
        except Exception:
            enrichments[table] = []

    # Fetch speakers
    try:
        surl = f"{base}/rest/v1/speakers?analysis_id=eq.{analysis_id}&select=*"
        sreq = Request(surl, headers=headers)
        speakers = json.loads(urlopen(sreq).read())
    except Exception:
        speakers = []

    # Build CaseBuddy Evidence object
    evidence = {
        "id": f"tubescribe-{analysis_id}",
        "name": a.get("title") or "YouTube Video Transcript",
        "type": "Digital Video Transcript",
        "description": (a.get("summary") or "")[:500],
        "dateObtained": a.get("captured_at") or a.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "exhibitNumber": "",  # To be assigned by CaseBuddy user
        "source": f"YouTube: {a.get('youtube_url', '')}",
        "status": "verified" if a.get("evidence_hash") else "unverified",
        "tags": ["tubescribe", "video-transcript", "digital-evidence"],
        "notes": f"Extracted via TubeScribe. Channel: {a.get('channel', 'Unknown')}."
    }

    # Build forensic data
    forensic = {
        "evidence_hash": a.get("evidence_hash"),
        "hash_algorithm": "SHA-256",
        "captured_at": a.get("captured_at"),
        "capture_metadata": a.get("capture_metadata"),
        "preserved_raw_hash": a.get("preserved_raw_hash"),
        "chain_of_custody": get_custody_chain(analysis_id),
        "integrity_status": "verified" if a.get("evidence_hash") else "pre-forensic"
    }

    # Build transcript pair
    transcript = {
        "raw": a.get("preserved_raw_transcript") or a.get("raw_transcript"),
        "polished": a.get("polished_transcript"),
    }

    # Build insights bundle
    sentiment = a.get("sentiment")
    if isinstance(sentiment, str):
        try: sentiment = json.loads(sentiment)
        except Exception: pass

    insights = {
        "summary": a.get("summary"),
        "expanded_notes": a.get("expanded_notes"),
        "sentiment": sentiment,
        "likely_production_date": a.get("likely_production_date"),
        "production_date_reasoning": a.get("production_date_reasoning"),
        "quotes": [{"text": q.get("quote_text"), "speaker": q.get("speaker"), "context": q.get("context"), "significance": q.get("significance")} for q in enrichments.get("quotes", [])],
        "timeline": [{"date": t.get("event_date"), "title": t.get("title"), "description": t.get("description"), "event_type": t.get("event_type")} for t in enrichments.get("timeline_events", [])],
        "contradictions": [{"statement_a": c.get("statement_a"), "statement_b": c.get("statement_b"), "explanation": c.get("explanation"), "severity": c.get("severity")} for c in enrichments.get("contradictions", [])],
        "facts": [{"fact": f.get("fact_text"), "category": f.get("category"), "confidence": f.get("confidence")} for f in enrichments.get("facts", [])],
        "entities": [{"name": e.get("entity_name"), "type": e.get("entity_type"), "context": e.get("context")} for e in enrichments.get("entities", [])],
        "chapters": [{"title": ch.get("title"), "summary": ch.get("summary"), "start_time": ch.get("start_seconds")} for ch in enrichments.get("chapters", [])],
    }

    # Build metadata
    metadata = {
        "source_url": a.get("youtube_url"),
        "youtube_id": a.get("youtube_id"),
        "channel": a.get("channel"),
        "thumbnail_url": a.get("thumbnail_url"),
        "speakers": [{"label": s.get("label"), "display_name": s.get("display_name")} for s in speakers],
        "tubescribe_analysis_id": analysis_id,
        "worker_version": WORKER_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }

    log_custody(analysis_id, "casebuddy_exported", {
        "format": "casebuddy_evidence_bundle",
    }, user_id=data.get("user_id"))

    return {
        "evidence": evidence,
        "forensic": forensic,
        "transcript": transcript,
        "insights": insights,
        "metadata": metadata,
    }


# ── MCP Server ──────────────────────────────────────────────────────────────
MCP_TOOLS = [
    {
        "name": "transcribe_video",
        "description": "Submit a YouTube video URL for transcription and AI analysis. Returns the analysis ID for tracking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "youtube_url": {"type": "string", "description": "YouTube video URL to transcribe"},
                "user_id": {"type": "string", "description": "Supabase user ID (optional)"},
            },
            "required": ["youtube_url"],
        },
    },
    {
        "name": "search_transcripts",
        "description": "Search across all transcribed videos for a text query. Returns matching analyses with snippets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query text"},
                "user_id": {"type": "string", "description": "Filter to a specific user's analyses"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_analysis",
        "description": "Get full analysis details by ID including summary, transcript, sentiment, and metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string", "description": "Analysis UUID"},
            },
            "required": ["analysis_id"],
        },
    },
    {
        "name": "get_quotes",
        "description": "Get notable quotes extracted from a video analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string", "description": "Analysis UUID"},
            },
            "required": ["analysis_id"],
        },
    },
    {
        "name": "get_timeline",
        "description": "Get timeline events extracted from a video analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string", "description": "Analysis UUID"},
            },
            "required": ["analysis_id"],
        },
    },
    {
        "name": "get_contradictions",
        "description": "Get contradictions found within a video analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string", "description": "Analysis UUID"},
            },
            "required": ["analysis_id"],
        },
    },
    {
        "name": "verify_evidence",
        "description": "Verify the forensic integrity of an analysis by re-computing its SHA-256 hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string", "description": "Analysis UUID"},
            },
            "required": ["analysis_id"],
        },
    },
]


def handle_mcp(data):
    """Handle MCP JSON-RPC 2.0 requests.
    
    Supports:
    - initialize: capability negotiation
    - tools/list: list available tools
    - tools/call: execute a tool
    """
    jsonrpc = data.get("jsonrpc", "2.0")
    method = data.get("method", "")
    params = data.get("params", {})
    req_id = data.get("id")

    def rpc_result(result):
        return {"jsonrpc": jsonrpc, "id": req_id, "result": result}

    def rpc_error(code, message):
        return {"jsonrpc": jsonrpc, "id": req_id, "error": {"code": code, "message": message}}

    # ── initialize ──
    if method == "initialize":
        return rpc_result({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "tubescribe-mcp",
                "version": WORKER_VERSION,
            },
        })

    # ── tools/list ──
    if method == "tools/list":
        return rpc_result({"tools": MCP_TOOLS})

    # ── tools/call ──
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return _mcp_call_tool(tool_name, arguments, rpc_result, rpc_error)

    return rpc_error(-32601, f"Method not found: {method}")


def _mcp_call_tool(tool_name, arguments, rpc_result, rpc_error):
    """Dispatch an MCP tool call and return the result."""
    headers = {"apikey": os.environ["SUPABASE_KEY"],
               "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
               "Content-Type": "application/json"}
    base = os.environ["SUPABASE_URL"]

    try:
        if tool_name == "transcribe_video":
            youtube_url = arguments.get("youtube_url", "")
            user_id = arguments.get("user_id")
            video_id = extract_video_id(youtube_url)
            if not video_id:
                return rpc_result({"content": [{"type": "text", "text": f"Invalid YouTube URL: {youtube_url}"}], "isError": True})
            
            # Insert a new analysis record
            insert_data = {
                "youtube_url": youtube_url,
                "youtube_id": video_id,
                "status": "pending",
                "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
            if user_id:
                insert_data["user_id"] = user_id
            
            req = Request(f"{base}/rest/v1/analyses", 
                         data=json.dumps(insert_data).encode(),
                         headers={**headers, "Prefer": "return=representation"}, method="POST")
            resp = json.loads(urlopen(req).read())
            new_id = resp[0]["id"] if isinstance(resp, list) else resp["id"]
            
            # Trigger the pipeline
            import threading
            threading.Thread(target=run_pipeline, args=(resp[0] if isinstance(resp, list) else resp,), daemon=True).start()
            
            return rpc_result({"content": [{"type": "text", "text": json.dumps({
                "analysis_id": new_id,
                "youtube_id": video_id,
                "status": "pending",
                "message": "Transcription pipeline started. Poll get_analysis to check status."
            })}]})

        elif tool_name == "search_transcripts":
            result = handle_search(arguments)
            return rpc_result({"content": [{"type": "text", "text": json.dumps(result)}]})

        elif tool_name == "get_analysis":
            analysis_id = arguments.get("analysis_id", "")
            url = f"{base}/rest/v1/analyses?id=eq.{analysis_id}&select=id,youtube_url,youtube_id,title,channel,status,summary,polished_transcript,expanded_notes,sentiment,likely_production_date,evidence_hash,captured_at,created_at"
            req = Request(url, headers=headers)
            resp = json.loads(urlopen(req).read())
            if not resp:
                return rpc_result({"content": [{"type": "text", "text": "Analysis not found"}], "isError": True})
            return rpc_result({"content": [{"type": "text", "text": json.dumps(resp[0])}]})

        elif tool_name == "get_quotes":
            analysis_id = arguments.get("analysis_id", "")
            url = f"{base}/rest/v1/quotes?analysis_id=eq.{analysis_id}&select=*"
            req = Request(url, headers=headers)
            resp = json.loads(urlopen(req).read())
            return rpc_result({"content": [{"type": "text", "text": json.dumps(resp)}]})

        elif tool_name == "get_timeline":
            analysis_id = arguments.get("analysis_id", "")
            url = f"{base}/rest/v1/timeline_events?analysis_id=eq.{analysis_id}&select=*&order=event_date"
            req = Request(url, headers=headers)
            resp = json.loads(urlopen(req).read())
            return rpc_result({"content": [{"type": "text", "text": json.dumps(resp)}]})

        elif tool_name == "get_contradictions":
            analysis_id = arguments.get("analysis_id", "")
            url = f"{base}/rest/v1/contradictions?analysis_id=eq.{analysis_id}&select=*"
            req = Request(url, headers=headers)
            resp = json.loads(urlopen(req).read())
            return rpc_result({"content": [{"type": "text", "text": json.dumps(resp)}]})

        elif tool_name == "verify_evidence":
            result = handle_verify_hash(arguments)
            return rpc_result({"content": [{"type": "text", "text": json.dumps(result)}]})

        else:
            return rpc_error(-32602, f"Unknown tool: {tool_name}")

    except Exception as e:
        return rpc_error(-32603, f"Tool execution error: {str(e)[:500]}")


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

# ── Supadata API (primary transcript source) ─────────────────────────────────
def _supadata_curl(url):
    """Call Supadata API via curl to bypass Cloudflare bot detection on urllib."""
    cmd = [
        "curl", "-s", "-m", "30",
        "-H", f"x-api-key: {SUPADATA_API_KEY}",
        "-H", "Accept: application/json",
        "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        if result.returncode != 0:
            print(f"[worker] Supadata curl failed (rc={result.returncode}): {result.stderr[:200]}")
            return None
        body = result.stdout.strip()
        if not body:
            print("[worker] Supadata curl returned empty body")
            return None
        return json.loads(body)
    except subprocess.TimeoutExpired:
        print("[worker] Supadata curl timed out")
        return None
    except json.JSONDecodeError as e:
        print(f"[worker] Supadata curl JSON error: {e}, body: {result.stdout[:200]}")
        return None
    except Exception as e:
        print(f"[worker] Supadata curl exception: {e}")
        return None


def fetch_supadata_transcript(video_id):
    """Fetch transcript via Supadata API — handles YouTube bot detection bypass."""
    if not SUPADATA_API_KEY:
        print("[worker] No SUPADATA_API_KEY set, skipping Supadata")
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
    """Single API call to Groq, Cerebras, or OpenRouter (no retries)."""
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
    if provider == "cerebras":
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
    """Route to best available AI: Cerebras → OpenRouter → Groq primary → Groq fallback.
    Staggers parallel calls to avoid rate limit bursts.
    If _track_model is a list, appends the name of the model that succeeded."""
    if not USE_CEREBRAS and not USE_OPENROUTER and not USE_GROQ:
        raise RuntimeError("No AI API key set — set CEREBRAS_API_KEY, OPENROUTER_API_KEY, or GROQ_API_KEY")
    # Stagger: Cerebras 0.3s, OpenRouter 0.5s, Groq 1.5s
    stagger = 0.3 if AI_PROVIDER == "cerebras" else (0.5 if AI_PROVIDER == "openrouter" else 1.5)
    with _ai_call_lock:
        now = time.time()
        wait_until = _ai_last_call[0] + stagger
        if now < wait_until:
            time.sleep(wait_until - now)
        _ai_last_call[0] = time.time()

    errors = []

    # Try Cerebras first if available (1M tokens/day free!)
    if USE_CEREBRAS:
        try:
            result = _call_with_retries(instructions, input_text, max_tokens, model=CEREBRAS_MODEL, provider="cerebras")
            if result:
                if isinstance(_track_model, list):
                    _track_model.append(f"Cerebras ({CEREBRAS_MODEL})")
                return result
            print(f"[call_openai] Cerebras returned empty, trying next")
        except Exception as e:
            errors.append(f"Cerebras: {str(e)[:200]}")
            print(f"[call_openai] Cerebras failed: {str(e)[:300]}, trying next")

    # Try OpenRouter (free models, generous limits)
    if USE_OPENROUTER:
        try:
            result = _call_with_retries(instructions, input_text, max_tokens, model=OPENROUTER_MODEL, provider="openrouter")
            if result:
                if isinstance(_track_model, list):
                    _track_model.append(f"OpenRouter ({OPENROUTER_MODEL})")
                return result
            print(f"[call_openai] OpenRouter returned empty, trying Groq")
        except Exception as e:
            errors.append(f"OpenRouter: {str(e)[:200]}")
            print(f"[call_openai] OpenRouter failed: {str(e)[:300]}, trying Groq")

    # Groq primary
    if USE_GROQ:
        try:
            result = _call_with_retries(instructions, input_text, max_tokens, model=GROQ_MODEL, provider="groq")
            if result:
                if isinstance(_track_model, list):
                    _track_model.append(f"Groq ({GROQ_MODEL})")
                return result
            print(f"[call_openai] {GROQ_MODEL} returned empty, trying fallback")
        except Exception as e:
            errors.append(f"Groq primary: {str(e)[:200]}")
            print(f"[call_openai] {GROQ_MODEL} failed: {str(e)[:300]}, trying fallback")

        # Groq fallback
        try:
            result = _call_with_retries(instructions, input_text, max_tokens, model=GROQ_FALLBACK, provider="groq")
            if result:
                if isinstance(_track_model, list):
                    _track_model.append(f"Groq ({GROQ_FALLBACK})")
                return result
            print(f"[call_openai] {GROQ_FALLBACK} also returned empty")
        except Exception as e:
            err_str = str(e)
            errors.append(f"Groq fallback: {err_str[:200]}")
            if "content_filter" in err_str.lower() or "moderation" in err_str.lower():
                print(f"[call_openai] Content filter triggered: {err_str[:300]}")
                if isinstance(_track_model, list):
                    _track_model.append("content_filter_fallback")
                return CONTENT_FILTER_FALLBACK
            print(f"[call_openai] {GROQ_FALLBACK} also failed: {err_str[:300]}")

    error_detail = "; ".join(errors) if errors else "No AI providers configured"
    if "daily token limit" in error_detail.lower():
        raise RuntimeError(f"Daily AI token limit reached. Resets at midnight UTC (~6 PM CST). ({error_detail})")
    raise RuntimeError(f"All AI models failed: {error_detail}")

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

Use these sections:
## Key Topics (bullet points)
## Top 3 Claims (who said what)
## People Mentioned (name and role)
## Notable Quotes (2-3 most significant)

Keep it brief and scannable."""
    elif note_length == "detailed":
        return f"""{research_ctx}\n\nProduce exhaustive, comprehensive expanded research notes from this video transcript. Extract EVERY piece of useful information. Leave nothing out.

Use these sections:
## Main Topics
(List and explain every topic discussed in depth, not just headlines)

## Key Claims & Allegations
(Every factual claim, allegation, or assertion made — include who said it, when, and any supporting evidence mentioned)

## People & Organizations
(Every person and organization mentioned, with their role, what was said about them, and relationships to other mentioned parties)

## Legal & Official Proceedings
(Any court cases, filings, hearings, laws, statutes, or official actions referenced — include case numbers if mentioned)

## Notable Quotes
(All significant direct quotes with full speaker attribution and context)

## Timeline of Events
(Detailed chronological sequence of all events discussed)

## Data & Statistics
(Any numbers, percentages, dates, or quantitative claims)

## Action Items & Next Steps
(Everything mentioned as needing to be done, by whom, and any deadlines)

## Unanswered Questions
(All questions raised but not answered in the video)

## Cross-References
(Connections to other events, people, or cases mentioned)

Be maximally exhaustive. A researcher using these notes should NEVER need to re-watch the video."""
    else:  # medium (default)
        return f"""{research_ctx}\n\nProduce comprehensive expanded research notes from this video transcript. Be thorough and extract ALL useful information.

Use these sections:
## Main Topics
(List and explain every topic discussed, not just headlines)

## Key Claims & Allegations
(Every factual claim, allegation, or assertion made — include who said it)

## People & Organizations
(Every person and organization mentioned, with their role and what was said about them)

## Legal & Official Proceedings
(Any court cases, filings, hearings, laws, or official actions referenced)

## Notable Quotes
(Direct quotes that are significant, with speaker attribution)

## Timeline of Events
(Chronological sequence of events discussed)

## Action Items & Next Steps
(Anything mentioned as needing to be done)

## Unanswered Questions
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

    prompts = [
        (f"{research_ctx}\n\nProduce a thorough summary of this video. Include: the main topic, all key points discussed, names of people and organizations mentioned, any legal proceedings or events described, and the overall significance. Be detailed — aim for 2-3 paragraphs, not just a few sentences.",
         f"{ctx}Transcript:\n{t_short}"),
        (f'{research_ctx}\n\nAnalyze the tone and return ONLY valid JSON (no markdown): {{"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}}',
         f"{ctx}Transcript:\n{t_short}"),
        (_get_notes_prompt(research_ctx, note_length),
         f"{ctx}Transcript:\n{t_medium}"),
        (f'{research_ctx}\n\nAnalyze for clues about when this content was produced. Return ONLY valid JSON (no markdown): {{"likely_production_date":"<date range>","reasoning":"<brief>"}}',
         f"{ctx}Transcript:\n{t_short}"),
        # 5th call: Speaker-aware polished transcript (placeholder — may be replaced by chunked version below)
        (f'''{research_ctx}

You are an expert transcript editor. Create a polished, readable version of this COMPLETE transcript for research documentation purposes. Do NOT truncate, summarize, or skip any part of the transcript.

Rules:
1. Identify different speakers from context clues (names mentioned, "I", "you", conversation flow, who is recording, etc.)
2. Label each speaker with their likely real name if identifiable, otherwise "Speaker 1", "Speaker 2", etc.
3. Fix obvious transcription errors, grammar issues, and filler words (um, uh, like)
4. Add paragraph breaks at natural topic shifts
5. Keep the meaning 100% accurate — never change what was said, only how it reads
6. Format as: **Speaker Name:** Their dialogue here...
7. Add [timestamp] markers every few paragraphs if timing info is available
8. Include EVERY part of the conversation from start to finish{speaker_ctx}

Return ONLY the polished transcript text, no other commentary. Do not skip or summarize any sections.''',
         f"{ctx}Transcript:\n{transcript[:60000]}"),
        # 6th call: Speaker identification JSON
        (f'''{research_ctx}

Identify all speakers in this transcript for research indexing. Return ONLY valid JSON (no markdown):
{{"speakers": [{{"label": "Speaker 1", "likely_name": "name or null", "role": "brief role description", "speaking_percentage": 0-100, "key_quotes": ["notable quote 1"]}}]}}

Look for: names mentioned in conversation, self-references, titles, the video creator/recorder.{speaker_ctx}''',
         f"{ctx}Transcript:\n{t_short}"),
    ]

    # Token limits per call: polished transcript & notes get 16K, others get 4K
    notes_tokens = {"short": 2000, "medium": 8000, "detailed": 16000}.get(note_length, 8000)
    token_limits = {0: 4000, 1: 2000, 2: notes_tokens, 3: 2000, 4: 16000, 5: 2000}

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
    print(f"[worker] 6 OpenAI calls completed in {elapsed:.1f}s (parallel) — models: {models_used}")
    summary, sentiment_raw, notes, date_raw, polished_text, speakers_raw = results

    # If transcript is very long, process polished transcript in chunks and combine
    if len(transcript) > 60000 and (not polished_text or len(polished_text) < len(transcript) * 0.3):
        print(f"[worker] Transcript is {len(transcript)} chars, processing polished transcript in chunks...")
        chunk_size = 50000
        chunks = [transcript[i:i+chunk_size] for i in range(0, len(transcript), chunk_size)]
        polished_parts = []
        for ci, chunk in enumerate(chunks):
            part = call_openai(
                f'''{research_ctx}\n\nYou are an expert transcript editor. Polish this section (part {ci+1} of {len(chunks)}) into clean, readable text. Fix grammar, add speaker labels, add paragraph breaks. Do NOT skip or summarize any content. Output ONLY the polished text.{speaker_ctx}''',
                f"{ctx}Transcript section {ci+1}/{len(chunks)}:\n{chunk}",
                16000
            )
            polished_parts.append(part)
        polished_text = "\n\n".join(polished_parts)
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
                if SUPADATA_API_KEY or DEEPGRAM_API_KEY:
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
            extract_facts_and_entities(analysis_id, user_id, title, polished, description)
            extract_quotes_and_timeline(analysis_id, user_id, title, polished, description)
            detect_contradictions(analysis_id, user_id, title, polished)
            log_custody(analysis_id, "enrichment_complete", {
                "enrichments": ["facts", "entities", "quotes", "timeline", "contradictions"],
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
def extract_facts_and_entities(analysis_id, user_id, title, transcript, description=""):
    """Extract factual claims and entities from a completed analysis — runs after main pipeline."""
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

                fact_rows.append({
                    "user_id": user_id,
                    "analysis_id": analysis_id,
                    "claim": f.get("claim", "")[:2000],
                    "category": f.get("category", "general"),
                    "source_timestamp": ts_seconds,
                    "citation": f.get("citation", ""),
                    "confidence": f.get("confidence", "medium"),
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
                out = _call_groq("You are a helpful assistant.", "Say 'AI working' in exactly two words.", 20, model=GROQ_MODEL)
                result["primary"] = {"status": "ok", "model": GROQ_MODEL, "response": out[:100]}
            except Exception as e:
                result["primary"] = {"status": "error", "model": GROQ_MODEL, "error": str(e)[:300]}
            try:
                out2 = _call_groq("You are a helpful assistant.", "Say 'AI working' in exactly two words.", 20, model=GROQ_FALLBACK)
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
        # ── MCP capabilities (GET) ──
        if path == "/mcp":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "name": "tubescribe-mcp",
                "version": WORKER_VERSION,
                "protocol": "MCP JSON-RPC 2.0",
                "description": "TubeScribe MCP server — transcribe YouTube videos, search transcripts, extract quotes/timeline/contradictions, verify forensic evidence integrity",
                "tools": MCP_TOOLS,
                "usage": "POST /mcp with JSON-RPC 2.0 body. Methods: initialize, tools/list, tools/call",
            }, indent=2).encode())
            return

        supadata = "yes" if SUPADATA_API_KEY else "no"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        if USE_CEREBRAS:
            ai_primary = f"cerebras ({CEREBRAS_MODEL})"
        elif USE_OPENROUTER:
            ai_primary = f"openrouter ({OPENROUTER_MODEL})"
        elif USE_GROQ:
            ai_primary = f"groq ({GROQ_MODEL})"
        else:
            ai_primary = "none"
        fallbacks = []
        if USE_OPENROUTER and AI_PROVIDER != "openrouter":
            fallbacks.append(f"openrouter ({OPENROUTER_MODEL})")
        if USE_GROQ:
            fallbacks.append(f"groq ({GROQ_MODEL})")
        ai_fallback = " → ".join(fallbacks) if fallbacks else "none"
        self.wfile.write(json.dumps({
            "status": "ok", "version": WORKER_VERSION, "supadata": supadata, "forensic": True, "mcp": True, "casebuddy_bridge": True,
            "ai_provider": AI_PROVIDER,
            "ai_primary": ai_primary,
            "ai_fallback": ai_fallback,
        }).encode())

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

        # ── CaseBuddy export endpoint ──
        if path == "/casebuddy-export":
            result = handle_casebuddy_export(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # ── MCP server endpoint ──
        if path == "/mcp":
            result = handle_mcp(payload)
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
        
        # Merge model info into existing raw_transcript JSON
        raw_rows = sb_get("analyses", {"id": analysis_id}, "raw_transcript")
        existing_raw = (raw_rows[0].get("raw_transcript") or {}) if raw_rows else {}
        existing_raw["ai_model_info"] = ai_model_info
        
        sb_patch("analyses", {"id": analysis_id}, {
            "status": "complete",
            "error_message": None,
            "raw_transcript": existing_raw,
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
    sections.append(f"# {a.get('title', 'Untitled Video')}")
    sections.append(f"Channel: {a.get('channel', 'Unknown')}")
    sections.append(f"URL: {a.get('youtube_url', '')}")
    if a.get("likely_production_date"):
        sections.append(f"Likely Recorded: {a['likely_production_date']}")
        if a.get("production_date_reasoning"):
            sections.append(f"Date Evidence: {a['production_date_reasoning']}")
    sections.append(f"Analyzed: {a.get('created_at', '')[:10]}")
    sections.append("")

    # Summary
    sections.append("## Summary")
    sections.append(a.get("summary", "N/A"))
    sections.append("")

    # Sentiment
    sections.append("## Sentiment Analysis")
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
        sections.append("## Identified Speakers")
        for sp in speakers_info:
            name = sp.get("likely_name") or sp.get("label", "Unknown")
            role = sp.get("role", "")
            pct = sp.get("speaking_percentage", "")
            sections.append(f"- **{name}**: {role} ({pct}% of dialogue)")
        sections.append("")

    # Polished Transcript
    sections.append("## Polished Transcript")
    sections.append(a.get("polished_transcript", "N/A"))
    sections.append("")

    # Speaker Utterances (raw with timestamps)
    if utts:
        sections.append("## Raw Speaker Transcript (with timestamps)")
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
    sections.append("## Expanded Notes")
    sections.append(a.get("expanded_notes", "N/A"))

    return {"text": "\n".join(sections), "title": a.get("title", "export")}

if __name__ == "__main__":
    print(f"[worker] {WORKER_VERSION} — Supadata: {'enabled' if SUPADATA_API_KEY else 'not configured'} | Forensic: enabled")
    print(f"[worker] Listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
