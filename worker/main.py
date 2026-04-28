"""
insightful-tube-explorer: Audio Worker v2
Handles: yt-dlp download → Azure Speech fast transcription
         → Azure OpenAI insights (parallel) → Supabase update

Key v2 improvements:
- Fast transcription API (synchronous, no polling, no blob upload)
- Parallel OpenAI calls (4x speedup)  
- Node.js runtime for yt-dlp + bot detection workarounds
- Granular timing logs
"""

import os, json, time, hmac, hashlib, base64, tempfile, subprocess, re, uuid, io
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor

# ── Env vars ────────────────────────────────────────────────────────────────
SUPABASE_URL             = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE    = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
AZURE_SPEECH_ENDPOINT    = os.environ.get("AZURE_SPEECH_ENDPOINT", "https://eastus.api.cognitive.microsoft.com/")
AZURE_SPEECH_KEY         = os.environ["AZURE_SPEECH_API_KEY"]
AZURE_STORAGE_CONN       = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
AZURE_STORAGE_ACCOUNT    = "wtptranscriptionstorage"
AZURE_STORAGE_CONTAINER  = "transcriptions"
AZURE_OPENAI_KEY         = os.environ["AZURE_OPENAI_API_KEY"]
AZURE_OPENAI_DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
PORT                     = int(os.environ.get("PORT", 8080))

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

# ── Azure Blob helpers (fallback for batch transcription) ────────────────────
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

# ── Azure Speech: Fast Transcription (synchronous, no polling) ───────────────
def parse_iso_duration(s):
    if not s:
        return 0.0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?', s)
    if not m:
        return 0.0
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = float(m.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds

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
    print(f"[worker] Fast transcription: sending {len(data)/1024/1024:.1f}MB to Azure Speech")
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
        error_body = e.read().decode()
        print(f"[worker] Fast transcription failed ({e.status}): {error_body}")
        raise RuntimeError(f"Fast transcription failed ({e.status}): {error_body}")

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
        end = start + duration
        utterances.append({"speaker": speaker, "text": text, "start": start, "end": end})
    return utterances

# ── Azure Speech: Batch Transcription (fallback) ────────────────────────────
def speech_headers():
    return {"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY, "Content-Type": "application/json"}

def submit_transcription_job(sas_url, analysis_id):
    endpoint = AZURE_SPEECH_ENDPOINT.rstrip("/")
    url = f"{endpoint}/speechtotext/v3.1/transcriptions"
    body = {
        "contentUrls": [sas_url], "locale": "en-US",
        "displayName": f"analysis-{analysis_id}",
        "properties": {"diarizationEnabled": True, "wordLevelTimestampsEnabled": False,
                        "punctuationMode": "DictatedAndAutomatic", "profanityFilterMode": "None"},
    }
    req = Request(url, data=json.dumps(body).encode(), headers=speech_headers(), method="POST")
    try:
        res = urlopen(req)
        return json.loads(res.read())["self"]
    except HTTPError as e:
        raise RuntimeError(f"Speech submit failed ({e.status}): {e.read()}")

def poll_transcription_job(job_url):
    for _ in range(120):
        time.sleep(5)
        req = Request(job_url, headers={"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY})
        data = json.loads(urlopen(req).read())
        if data["status"] == "Succeeded":
            return data
        if data["status"] == "Failed":
            raise RuntimeError(f"Speech job failed: {data}")
    raise RuntimeError("Speech job timed out")

def fetch_transcription_results(job_data):
    files_url = job_data["links"]["files"]
    req = Request(files_url, headers={"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY})
    files_data = json.loads(urlopen(req).read())
    file = next((f for f in files_data.get("values", []) if f["kind"] == "Transcription"), None)
    if not file:
        raise RuntimeError("No transcription file found")
    return json.loads(urlopen(file["links"]["contentUrl"]).read())

def parse_batch_utterances(result):
    utterances = []
    for phrase in result.get("recognizedPhrases", []):
        best = phrase.get("nBest", [{}])[0]
        text = best.get("display", "")
        if not text:
            continue
        speaker = f"Speaker {phrase['speaker']}" if phrase.get("speaker") is not None else "Unknown"
        start = phrase.get("offsetInTicks", 0) / 1e7
        end = (phrase.get("offsetInTicks", 0) + phrase.get("durationInTicks", 0)) / 1e7
        utterances.append({"speaker": speaker, "text": text, "start": start, "end": end})
    return utterances

# ── Azure OpenAI (Responses API) ─────────────────────────────────────────────
OPENAI_URL = "https://openaiyoutube.openai.azure.com/openai/responses?api-version=2025-04-01-preview"

def call_openai(instructions, input_text):
    body = {
        "model": AZURE_OPENAI_DEPLOYMENT, "instructions": instructions,
        "input": input_text, "max_output_tokens": 2000, "temperature": 0.3,
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

def generate_insights(transcript, title):
    ctx = f'Video title: "{title}"\n\n' if title else ""
    t = transcript[:12000]
    prompts = [
        ("You are an expert media analyst. Produce a concise 3-5 sentence summary of the key points discussed.", f"{ctx}Transcript:\n{t}"),
        ('Analyze and return ONLY valid JSON (no markdown): {"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}', f"{ctx}Transcript:\n{t}"),
        ("Produce detailed expanded notes with sections: ## Main Topics, ## Key Claims, ## Notable Quotes, ## Action Items, ## Unanswered Questions", f"{ctx}Transcript:\n{t}"),
        ('Analyze for clues about when content was produced. Return ONLY valid JSON (no markdown): {"likely_production_date":"<date range>","reasoning":"<brief>"}', f"{ctx}Transcript:\n{transcript[:8000]}"),
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

# ── yt-dlp download with bot-detection workarounds ──────────────────────────
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

def download_audio(youtube_url, audio_path):
    """Try multiple yt-dlp strategies to handle YouTube bot detection."""
    strategies = [
        # Strategy 1: web_creator client (least likely to trigger bot detection)
        ["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
         "--audio-quality", "96K",
         "--extractor-args", "youtube:player_client=web_creator",
         "--user-agent", USER_AGENT,
         "--no-check-certificates",
         "-o", audio_path, youtube_url],
        # Strategy 2: mediaconnect client
        ["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
         "--audio-quality", "96K",
         "--extractor-args", "youtube:player_client=mediaconnect",
         "--user-agent", USER_AGENT,
         "--no-check-certificates",
         "-o", audio_path, youtube_url],
        # Strategy 3: android_vr client + geo bypass
        ["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
         "--audio-quality", "96K",
         "--extractor-args", "youtube:player_client=android_vr",
         "--user-agent", USER_AGENT,
         "--geo-bypass",
         "--no-check-certificates",
         "-o", audio_path, youtube_url],
        # Strategy 4: default with geo bypass
        ["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
         "--audio-quality", "96K",
         "--user-agent", USER_AGENT,
         "--geo-bypass",
         "--no-check-certificates",
         "-o", audio_path, youtube_url],
    ]
    last_err = ""
    for i, cmd in enumerate(strategies):
        client = cmd[cmd.index("--extractor-args") + 1].split("=")[1] if "--extractor-args" in cmd else "default"
        print(f"[worker] Download attempt {i+1}/4 (client={client})")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(audio_path):
            print(f"[worker] Download succeeded with strategy {i+1}")
            return
        last_err = result.stderr
        print(f"[worker] Strategy {i+1} failed: {last_err[:200]}")
        # Clean up partial file
        if os.path.exists(audio_path):
            os.remove(audio_path)

    raise RuntimeError(f"yt-dlp failed all strategies: {last_err}")

# ── Pipeline ─────────────────────────────────────────────────────────────────
MAX_FAST_TRANSCRIBE_SIZE = 200 * 1024 * 1024

def run_pipeline(record):
    analysis_id = record["id"]
    youtube_url = record["youtube_url"]
    title = record.get("title")
    user_id = record.get("user_id")
    t_start = time.time()

    print(f"[worker] Starting pipeline for {analysis_id}")

    with tempfile.TemporaryDirectory() as tmp:
        audio_path = f"{tmp}/audio.mp3"
        blob_name = f"{analysis_id}.mp3"

        try:
            # 1. Download audio
            set_status(analysis_id, "extracting")
            t0 = time.time()
            download_audio(youtube_url, audio_path)
            file_size = os.path.getsize(audio_path)
            print(f"[worker] Downloaded audio in {time.time()-t0:.1f}s ({file_size/1024/1024:.1f}MB)")

            # 2. Transcription
            set_status(analysis_id, "transcribing")
            t0 = time.time()
            use_fast = file_size < MAX_FAST_TRANSCRIBE_SIZE
            if use_fast:
                try:
                    result_data = fast_transcribe(audio_path, analysis_id)
                    utterances = parse_fast_utterances(result_data)
                    print(f"[worker] Fast transcription: {len(utterances)} utterances in {time.time()-t0:.1f}s")
                except Exception as e:
                    print(f"[worker] Fast transcription failed, falling back to batch: {e}")
                    use_fast = False

            if not use_fast:
                print(f"[worker] Using batch transcription (file={file_size/1024/1024:.1f}MB)")
                upload_blob(audio_path, blob_name)
                sas_url = generate_sas_url(blob_name)
                job_url = submit_transcription_job(sas_url, analysis_id)
                job_data = poll_transcription_job(job_url)
                result_data = fetch_transcription_results(job_data)
                utterances = parse_batch_utterances(result_data)
                print(f"[worker] Batch transcription: {len(utterances)} utterances in {time.time()-t0:.1f}s")

            polished = "\n".join(f"[{u['speaker']}]: {u['text']}" for u in utterances)

            # 3. AI insights (parallel)
            set_status(analysis_id, "processing")
            insights = generate_insights(polished, title)

            # 4. Update analyses
            sb_patch("analyses", {"id": analysis_id}, {
                "status": "complete",
                "raw_transcript": result_data,
                "polished_transcript": polished,
                **insights,
            })

            # 5. Insert utterances
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
            print(f"[worker] Pipeline complete in {total:.1f}s for {analysis_id}")

        except Exception as e:
            fail_analysis(analysis_id, str(e))

# ── HTTP Server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

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
    print(f"[worker] v2 Listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
