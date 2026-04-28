"""
TubeScribe: Audio Worker v6
Pipeline: Pasted transcript → Supadata API → YouTube captions fallback
         → Azure OpenAI insights (parallel)

v6: Supadata API integration — bypasses YouTube bot detection entirely
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
AZURE_OPENAI_KEY         = os.environ["AZURE_OPENAI_API_KEY"]
AZURE_OPENAI_DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
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

# ── Azure OpenAI ─────────────────────────────────────────────────────────────
OPENAI_URL = "https://openaiyoutube.openai.azure.com/openai/responses?api-version=2025-04-01-preview"

def call_openai(instructions, input_text):
    body = {
        "model": AZURE_OPENAI_DEPLOYMENT, "instructions": instructions,
        "input": input_text, "max_output_tokens": 2000,
    }
    req = Request(OPENAI_URL, data=json.dumps(body).encode(), headers={
        "api-key": AZURE_OPENAI_KEY, "Content-Type": "application/json",
    }, method="POST")
    try:
        data = json.loads(urlopen(req).read())
    except HTTPError as e:
        raise RuntimeError(f"Azure OpenAI failed ({e.status}): {e.read()}")
    for item in data.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    return block["text"]
    return data.get("output_text", "")

def generate_insights(transcript, title, description=""):
    ctx = f'Video title: "{title}"\n\n' if title else ""
    if description:
        ctx += f'Video description: "{description[:500]}"\n\n'
    t = transcript[:12000]
    prompts = [
        ("You are an expert media analyst. Produce a concise 3-5 sentence summary of the key points discussed.",
         f"{ctx}Transcript:\n{t}"),
        ('Analyze and return ONLY valid JSON (no markdown): {"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}',
         f"{ctx}Transcript:\n{t}"),
        ("Produce detailed expanded notes with sections: ## Main Topics, ## Key Claims, ## Notable Quotes, ## Action Items, ## Unanswered Questions",
         f"{ctx}Transcript:\n{t}"),
        ('Analyze for clues about when content was produced. Return ONLY valid JSON (no markdown): {"likely_production_date":"<date range>","reasoning":"<brief>"}',
         f"{ctx}Transcript:\n{transcript[:8000]}"),
    ]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(call_openai, p[0], p[1]) for p in prompts]
        results = [f.result() for f in futures]
    print(f"[worker] 4 OpenAI calls completed in {time.time()-t0:.1f}s (parallel)")
    summary, sentiment_raw, notes, date_raw = results

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

    return {
        "summary": summary, "sentiment": sentiment, "expanded_notes": notes,
        "likely_production_date": likely_date, "production_date_reasoning": date_reasoning,
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
        insights = generate_insights(polished, title, description)

        # ── Save ──
        sb_patch("analyses", {"id": analysis_id}, {
            "status": "complete",
            "raw_transcript": raw_data,
            "polished_transcript": polished,
            **insights,
        })

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

        total = time.time() - t_start
        print(f"[worker] ✅ Complete in {total:.1f}s ({used_source}) for {analysis_id}")

    except Exception as e:
        fail_analysis(analysis_id, str(e))

# ── HTTP Server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        supadata = "yes" if SUPADATA_API_KEY else "no"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "ok", "version": "v6", "supadata": supadata
        }).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        record = payload.get("record", payload)
        if record.get("status", "pending") != "pending":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"skipped":true}')
            return

        import threading
        threading.Thread(target=run_pipeline, args=(record,), daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "id": record.get("id")}).encode())

    def log_message(self, format, *args):
        print(f"[http] {args[0]} {args[1]}")

if __name__ == "__main__":
    print(f"[worker] v6 — Supadata: {'enabled' if SUPADATA_API_KEY else 'not configured'}")
    print(f"[worker] Listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
