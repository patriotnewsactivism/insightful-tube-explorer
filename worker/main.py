"""
TubeScribe: Audio Worker v8
Pipeline: Pasted transcript → Supadata API → YouTube captions fallback
         → Azure OpenAI insights (parallel)

v6: Supadata API integration
v7: Speaker ID, polished transcript, AI chat, export
v8: Fact extraction, entity extraction, cross-video search, bulk support
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

def call_openai(instructions, input_text, max_tokens=2000):
    body = {
        "model": AZURE_OPENAI_DEPLOYMENT, "instructions": instructions,
        "input": input_text, "max_output_tokens": max_tokens,
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
    t = transcript[:12000]

    # Get known speakers for context
    known_speakers = get_known_speakers(user_id) if user_id else []
    speaker_ctx = ""
    if known_speakers:
        names = ", ".join(s["name"] for s in known_speakers[:20])
        speaker_ctx = f"\n\nKnown speakers from previous videos: {names}. Try to match voices/speakers to these known people if they appear in this video."

    prompts = [
        ("You are an expert media analyst. Produce a concise 3-5 sentence summary of the key points discussed.",
         f"{ctx}Transcript:\n{t}"),
        ('Analyze and return ONLY valid JSON (no markdown): {"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}',
         f"{ctx}Transcript:\n{t}"),
        ("Produce detailed expanded notes with sections: ## Main Topics, ## Key Claims, ## Notable Quotes, ## Action Items, ## Unanswered Questions",
         f"{ctx}Transcript:\n{t}"),
        ('Analyze for clues about when content was produced. Return ONLY valid JSON (no markdown): {"likely_production_date":"<date range>","reasoning":"<brief>"}',
         f"{ctx}Transcript:\n{transcript[:8000]}"),
        # 5th call: Speaker-aware polished transcript
        (f'''You are an expert transcript editor. Create a polished, readable version of this transcript.

Rules:
1. Identify different speakers from context clues (names mentioned, "I", "you", conversation flow, who is recording, etc.)
2. Label each speaker with their likely real name if identifiable, otherwise "Speaker 1", "Speaker 2", etc.
3. Fix obvious transcription errors, grammar issues, and filler words (um, uh, like)
4. Add paragraph breaks at natural topic shifts
5. Keep the meaning 100% accurate — never change what was said, only how it reads
6. Format as: **Speaker Name:** Their dialogue here...
7. Add [timestamp] markers every few paragraphs if timing info is available{speaker_ctx}

Return ONLY the polished transcript text, no other commentary.''',
         f"{ctx}Transcript:\n{transcript[:14000]}"),
        # 6th call: Speaker identification JSON
        (f'''Identify all speakers in this transcript. Return ONLY valid JSON (no markdown):
{{"speakers": [{{"label": "Speaker 1", "likely_name": "name or null", "role": "brief role description", "speaking_percentage": 0-100, "key_quotes": ["notable quote 1"]}}]}}

Look for: names mentioned in conversation, self-references, titles, the video creator/recorder.{speaker_ctx}''',
         f"{ctx}Transcript:\n{t}"),
    ]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(call_openai, p[0], p[1], 4000 if i == 4 else 2000) for i, p in enumerate(prompts)]
        results = [f.result() for f in futures]
    print(f"[worker] 6 OpenAI calls completed in {time.time()-t0:.1f}s (parallel)")
    summary, sentiment_raw, notes, date_raw, polished_text, speakers_raw = results

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

        # ── Extract facts & entities (async, after completion) ──
        import threading as _th
        _th.Thread(
            target=extract_facts_and_entities,
            args=(analysis_id, user_id, title, polished, description),
            daemon=True,
        ).start()

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
    t = transcript[:14000]

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
            futures = [executor.submit(call_openai, p[0], p[1], 4000) for p in prompts]
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
{(analysis.get('polished_transcript') or '')[:8000]}

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
