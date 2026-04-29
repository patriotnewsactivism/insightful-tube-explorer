"""
TubeScribe: Audio Worker v8
Pipeline: Pasted transcript → Supadata API → YouTube captions fallback
         → Azure OpenAI insights (parallel)

v6: Supadata API integration
v7: Speaker ID, polished transcript, AI chat, export
v8: Fact extraction, entity extraction, cross-video search, bulk support
v8.1: Quote extraction, timeline builder, contradiction detector
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
AZURE_SPEECH_ENDPOINT    = os.environ.get("AZURE_SPEECH_ENDPOINT", "https://eastus.api.cognitive.microsoft.com/")
AZURE_SPEECH_KEY         = os.environ.get("AZURE_SPEECH_API_KEY", "")
AZURE_STORAGE_CONN       = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_ACCOUNT    = "wtptranscriptionstorage"
AZURE_STORAGE_CONTAINER  = "transcriptions"
AZURE_OPENAI_KEY         = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

# ── Grok (primary model via Azure AI Foundry) ───────────────────────────────
GROK_ENDPOINT = os.environ.get("GROK_ENDPOINT", "https://patri-mojrzk25-swedencentral.services.ai.azure.com/models/chat/completions?api-version=2024-05-01-preview")
GROK_API_KEY  = os.environ.get("GROK_API_KEY", "")
GROK_MODEL    = os.environ.get("GROK_MODEL", "grok-4-1-fast-reasoning")
USE_GROK      = bool(GROK_API_KEY)  # auto-enable if key is set

# ── DeepSeek V3.2 (fallback model via Azure AI Foundry) ─────────────────────
DEEPSEEK_ENDPOINT = os.environ.get("DEEPSEEK_ENDPOINT", "https://patri-moar8a1w-eastus2.services.ai.azure.com/models/chat/completions?api-version=2024-05-01-preview")
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL    = os.environ.get("DEEPSEEK_MODEL", "DeepSeek-V3-2")
USE_DEEPSEEK      = bool(DEEPSEEK_API_KEY)  # auto-enable if key is set
SUPADATA_API_KEY         = os.environ.get("SUPADATA_API_KEY", "")
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
OPENAI_URL = "https://openaiyoutube.openai.azure.com/openai/responses?api-version=2025-04-01-preview"

CONTENT_FILTER_FALLBACK = "[Content filtered by Azure — this section could not be analyzed due to content policy restrictions on the transcript material.]"


def _call_grok(instructions, input_text, max_tokens=2000):
    """Call Grok via Azure AI Foundry (standard chat/completions format)."""
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if input_text:
        messages.append({"role": "user", "content": input_text})
    body = {
        "model": GROK_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    req = Request(GROK_ENDPOINT, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json",
    }, method="POST")
    data = json.loads(urlopen(req, timeout=120).read())
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _call_deepseek(instructions, input_text, max_tokens=2000):
    """Call DeepSeek V3.2 via Azure AI Foundry Chat Completions API (fallback)."""
    body = {
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": input_text},
        ],
        "max_tokens": max_tokens,
        "model": DEEPSEEK_MODEL,
    }
    req = Request(DEEPSEEK_ENDPOINT, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }, method="POST")
    data = json.loads(urlopen(req, timeout=120).read())
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _call_azure_openai(instructions, input_text, max_tokens=2000):
    """Call Azure OpenAI Responses API (legacy fallback)."""
    body = {
        "model": AZURE_OPENAI_DEPLOYMENT, "instructions": instructions,
        "input": input_text, "max_output_tokens": max_tokens,
    }
    req = Request(OPENAI_URL, data=json.dumps(body).encode(), headers={
        "api-key": AZURE_OPENAI_KEY, "Content-Type": "application/json",
    }, method="POST")
    data = json.loads(urlopen(req).read())
    for item in data.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    return block["text"]
    return data.get("output_text", "")


def call_openai(instructions, input_text, max_tokens=2000):
    """Route to Grok (primary) → DeepSeek V3.2 (fallback) → Azure OpenAI (last resort)."""
    if USE_GROK:
        try:
            result = _call_grok(instructions, input_text, max_tokens)
            if result:
                return result
            print("[call_openai] Grok returned empty, falling back to DeepSeek V3.2")
        except Exception as e:
            err_str = str(e)
            print(f"[call_openai] Grok failed: {err_str[:300]}, falling back to DeepSeek V3.2")
    # Fallback 1: DeepSeek V3.2
    if USE_DEEPSEEK:
        try:
            result = _call_deepseek(instructions, input_text, max_tokens)
            if result:
                return result
            print("[call_openai] DeepSeek returned empty, falling back to Azure OpenAI")
        except Exception as e:
            err_str = str(e)
            print(f"[call_openai] DeepSeek failed: {err_str[:300]}, falling back to Azure OpenAI")
    # Fallback 2: Azure OpenAI (last resort)
    try:
        return _call_azure_openai(instructions, input_text, max_tokens)
    except HTTPError as e:
        body_bytes = e.read()
        body_str = body_bytes.decode("utf-8", errors="replace") if isinstance(body_bytes, bytes) else str(body_bytes)
        if e.status == 400 and "content_filter" in body_str:
            print(f"[call_openai] Content filter triggered, returning fallback. Details: {body_str[:300]}")
            return CONTENT_FILTER_FALLBACK
        raise RuntimeError(f"Azure OpenAI failed ({e.status}): {body_str}")

def get_known_speakers(user_id):
    """Fetch known speakers for this user to help with identification."""
    try:
        rows = sb_get("speakers", {"user_id": user_id}, "id,name,channel,notes")
        return rows if rows else []
    except Exception:
        return []

def generate_insights(transcript, title, description="", user_id=None):
    ctx = f'Video title: "{title}"\n\n' if title else ""
    if description:
        ctx += f'Video description: "{description[:500]}"\n\n'
    t = transcript[:60000]

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
         f"{ctx}Transcript:\n{t}"),
        (f'{research_ctx}\n\nAnalyze the tone and return ONLY valid JSON (no markdown): {{"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}}',
         f"{ctx}Transcript:\n{t}"),
        (f"""{research_ctx}\n\nProduce comprehensive expanded research notes from this video transcript. Be thorough and extract ALL useful information.

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

Be exhaustive. A researcher using these notes should not need to re-watch the video.""",
         f"{ctx}Transcript:\n{t}"),
        (f'{research_ctx}\n\nAnalyze for clues about when this content was produced. Return ONLY valid JSON (no markdown): {{"likely_production_date":"<date range>","reasoning":"<brief>"}}',
         f"{ctx}Transcript:\n{t}"),
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
         f"{ctx}Transcript:\n{t}"),
    ]

    # Token limits per call: polished transcript & notes get 16K, others get 4K
    token_limits = {0: 4000, 1: 2000, 2: 8000, 3: 2000, 4: 16000, 5: 2000}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(call_openai, p[0], p[1], token_limits.get(i, 4000)) for i, p in enumerate(prompts)]
        results = [f.result() for f in futures]
    print(f"[worker] 6 OpenAI calls completed in {time.time()-t0:.1f}s (parallel)")
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
            polished = "\n".join(seg["text"] for seg in segments)
            raw_data = {"source": "pasted_transcript", "char_count": len(pasted), "segments": len(segments)}
            used_source = "pasted transcript"

        else:
            video_id = extract_video_id(youtube_url)
            if not video_id:
                fail_analysis(analysis_id, "Could not extract video ID from URL")
                return

            # ── TRACK 1: Supadata API (primary — bypasses YouTube bot detection) ──
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
                    polished = "\n".join(seg["text"] for seg in segments)
                    raw_data = {"source": "supadata_api", "segments": len(segments),
                                "video_info": video_info if video_info else {}}
                    used_source = "Supadata API"
                else:
                    # Supadata returned data but we couldn't parse segments
                    # Try treating the whole response as text
                    if isinstance(transcript_data, str) and len(transcript_data.strip()) > 50:
                        text = transcript_data.strip()
                        utterances = [{"speaker": "Narrator", "text": text, "start": 0.0, "end": len(text.split()) * 0.4}]
                        polished = text
                        raw_data = {"source": "supadata_api_text", "char_count": len(text)}
                        used_source = "Supadata API (plain text)"
                    else:
                        transcript_data = None  # Fall through to next track

            if not transcript_data or (not segments if 'segments' in dir() else True):
                # ── TRACK 2: YouTube transcript (direct scrape, may be blocked) ──
                yt_transcript = fetch_youtube_transcript(video_id) if video_id else None

                if yt_transcript and len(yt_transcript) > 5:
                    print(f"[worker] Using YouTube transcript ({len(yt_transcript)} segments)")
                    set_status(analysis_id, "transcribing")
                    utterances = [
                        {"speaker": "Narrator", "text": seg["text"], "start": seg["start"], "end": seg["end"]}
                        for seg in yt_transcript
                    ]
                    polished = "\n".join(seg["text"] for seg in yt_transcript)
                    raw_data = {"source": "youtube_captions", "segments": yt_transcript}
                    used_source = "YouTube captions"
                else:
                    # ── TRACK 3: Fail with helpful message ──
                    if SUPADATA_API_KEY:
                        fail_analysis(analysis_id,
                            "Could not extract transcript. The Supadata API may be out of credits, "
                            "or this video has no available captions. Try the 'Paste transcript' option.")
                    else:
                        fail_analysis(analysis_id,
                            "YouTube blocks server access for transcript extraction. "
                            "Please use the 'Paste transcript manually' option on the dashboard.")
                    return

        # ── AI Insights ──
        set_status(analysis_id, "processing")
        insights = generate_insights(polished, title, description, user_id=user_id)

        # Extract speakers_info before saving (not a DB column)
        speakers_info = insights.pop("speakers_info", [])

        # ── Save ──
        sb_patch("analyses", {"id": analysis_id}, {
            "status": "complete",
            "raw_transcript": {**raw_data, "speakers_info": speakers_info},
            **insights,
        })

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
        supadata = "yes" if SUPADATA_API_KEY else "no"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "ok", "version": "v8", "supadata": supadata
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
    if not analysis_id:
        return {"error": "analysis_id required"}
    
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
    
    print(f"[reprocess] Starting insights for {analysis_id[:8]} ({len(transcript)} chars)")
    set_status(analysis_id, "processing")
    
    try:
        insights = generate_insights(transcript, title, user_id=user_id)
        speakers_info = insights.pop("speakers_info", [])
        
        sb_patch("analyses", {"id": analysis_id}, {
            "status": "complete",
            "error_message": None,
            **insights,
        })
        
        if speakers_info and user_id:
            save_identified_speakers(user_id, analysis_id, speakers_info)
        
        print(f"[reprocess] Completed {analysis_id[:8]}")
        return {"ok": True, "id": analysis_id}
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
    print(f"[worker] v8 — Supadata: {'enabled' if SUPADATA_API_KEY else 'not configured'}")
    print(f"[worker] Listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
