"""
insightful-tube-explorer: Audio Worker
Handles: yt-dlp download → Azure Blob upload → Azure Speech transcription
         → Azure OpenAI insights → Supabase update

Deploy on Railway (no CLI needed — just connect GitHub repo, set env vars, done).
"""

import os, json, time, hmac, hashlib, base64, tempfile, subprocess, re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, HTTPServer

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

def sb_patch(table: str, match: dict, data: dict):
    params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    req = Request(url, data=json.dumps(data).encode(), headers=sb_headers(), method="PATCH")
    try:
        urlopen(req)
    except HTTPError as e:
        print(f"[sb_patch] {e.status}: {e.read()}")

def set_status(analysis_id: str, status: str, extra: dict = {}):
    sb_patch("analyses", {"id": analysis_id}, {"status": status, **extra})

def fail_analysis(analysis_id: str, message: str):
    print(f"[worker] FAILED {analysis_id}: {message}")
    sb_patch("analyses", {"id": analysis_id}, {"status": "failed", "error_message": message[:2000]})

def sb_insert(table: str, rows: list):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**sb_headers(), "Prefer": "return=minimal"}
    req = Request(url, data=json.dumps(rows).encode(), headers=headers, method="POST")
    try:
        urlopen(req)
    except HTTPError as e:
        print(f"[sb_insert] {e.status}: {e.read()}")

# ── Azure Blob helpers ───────────────────────────────────────────────────────
def parse_conn_str(cs: str) -> dict:
    result = {}
    for part in cs.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k] = v
    return result

def hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()

def upload_blob(file_path: str, blob_name: str):
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
        "Authorization": auth,
        "x-ms-blob-type": "BlockBlob",
        "x-ms-date": date,
        "x-ms-version": "2020-04-08",
        "Content-Type": content_type,
        "Content-Length": str(len(file_data)),
    })
    try:
        urlopen(req)
    except HTTPError as e:
        raise RuntimeError(f"Blob upload failed ({e.status}): {e.read()}")

def generate_sas_url(blob_name: str) -> str:
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

# ── Azure Speech helpers ─────────────────────────────────────────────────────
def speech_headers():
    return {"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY, "Content-Type": "application/json"}

def submit_transcription_job(sas_url: str, analysis_id: str) -> str:
    endpoint = AZURE_SPEECH_ENDPOINT.rstrip("/")
    url = f"{endpoint}/speechtotext/v3.1/transcriptions"
    body = {
        "contentUrls": [sas_url],
        "locale": "en-US",
        "displayName": f"analysis-{analysis_id}",
        "properties": {
            "diarizationEnabled": True,
            "wordLevelTimestampsEnabled": False,
            "punctuationMode": "DictatedAndAutomatic",
            "profanityFilterMode": "None",
        },
    }
    req = Request(url, data=json.dumps(body).encode(), headers=speech_headers(), method="POST")
    try:
        res = urlopen(req)
        data = json.loads(res.read())
        return data["self"]
    except HTTPError as e:
        raise RuntimeError(f"Speech submit failed ({e.status}): {e.read()}")

def poll_transcription_job(job_url: str) -> dict:
    for _ in range(120):
        time.sleep(5)
        req = Request(job_url, headers={"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY})
        res = urlopen(req)
        data = json.loads(res.read())
        if data["status"] == "Succeeded":
            return data
        if data["status"] == "Failed":
            raise RuntimeError(f"Speech job failed: {data}")
    raise RuntimeError("Speech job timed out")

def fetch_transcription_results(job_data: dict) -> dict:
    files_url = job_data["links"]["files"]
    req = Request(files_url, headers={"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY})
    files_data = json.loads(urlopen(req).read())
    file = next((f for f in files_data.get("values", []) if f["kind"] == "Transcription"), None)
    if not file:
        raise RuntimeError("No transcription file found")
    return json.loads(urlopen(file["links"]["contentUrl"]).read())

def parse_utterances(result: dict) -> list:
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

def call_openai(instructions: str, input_text: str) -> str:
    body = {
        "model": AZURE_OPENAI_DEPLOYMENT,
        "instructions": instructions,
        "input": input_text,
        "max_output_tokens": 2000,
        "temperature": 0.3,
    }
    req = Request(OPENAI_URL, data=json.dumps(body).encode(), headers={
        "api-key": AZURE_OPENAI_KEY,
        "Content-Type": "application/json",
    }, method="POST")
    try:
        data = json.loads(urlopen(req).read())
    except HTTPError as e:
        raise RuntimeError(f"Azure OpenAI failed ({e.status}): {e.read()}")

    # Responses API: output is array of message objects
    for item in data.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    return block["text"]
    return data.get("output_text", "")

def generate_insights(transcript: str, title: str | None) -> dict:
    ctx = f'Video title: "{title}"\n\n' if title else ""
    t = transcript[:12000]

    summary = call_openai(
        "You are an expert media analyst. Produce a concise 3–5 sentence summary of the key points discussed.",
        f"{ctx}Transcript:\n{t}"
    )
    sentiment_raw = call_openai(
        'Analyze and return ONLY valid JSON (no markdown): {"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}',
        f"{ctx}Transcript:\n{t}"
    )
    notes = call_openai(
        "Produce detailed expanded notes with sections: ## Main Topics, ## Key Claims, ## Notable Quotes, ## Action Items, ## Unanswered Questions",
        f"{ctx}Transcript:\n{t}"
    )
    date_raw = call_openai(
        'Analyze for clues about when content was produced. Return ONLY valid JSON (no markdown): {"likely_production_date":"<date range>","reasoning":"<brief>"}',
        f"{ctx}Transcript:\n{transcript[:8000]}"
    )

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
        "summary": summary,
        "sentiment": sentiment,
        "expanded_notes": notes,
        "likely_production_date": likely_date,
        "production_date_reasoning": date_reasoning,
    }

# ── Pipeline ─────────────────────────────────────────────────────────────────
def run_pipeline(record: dict):
    analysis_id = record["id"]
    youtube_url = record["youtube_url"]
    title = record.get("title")
    user_id = record.get("user_id")

    print(f"[worker] Starting pipeline for {analysis_id}")

    with tempfile.TemporaryDirectory() as tmp:
        audio_path = f"{tmp}/audio.mp3"
        blob_name = f"{analysis_id}.mp3"

        try:
            # 1. Download audio
            set_status(analysis_id, "extracting")
            result = subprocess.run(
                ["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
                 "--audio-quality", "96K", "-o", audio_path, youtube_url],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                raise RuntimeError(f"yt-dlp failed: {result.stderr}")

            # 2. Upload to Azure Blob
            set_status(analysis_id, "transcribing")
            upload_blob(audio_path, blob_name)
            sas_url = generate_sas_url(blob_name)

            # 3. Speech transcription
            job_url = submit_transcription_job(sas_url, analysis_id)
            job_data = poll_transcription_job(job_url)
            result_data = fetch_transcription_results(job_data)

            utterances = parse_utterances(result_data)
            polished = "\n".join(f"[{u['speaker']}]: {u['text']}" for u in utterances)

            # 4. AI insights
            set_status(analysis_id, "processing")
            insights = generate_insights(polished, title)

            # 5. Update analyses
            sb_patch("analyses", {"id": analysis_id}, {
                "status": "complete",
                "raw_transcript": result_data,
                "polished_transcript": polished,
                **insights,
            })

            # 6. Insert utterances
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

            print(f"[worker] Pipeline complete ✓ {analysis_id}")

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

        # Run pipeline in background thread
        import threading
        threading.Thread(target=run_pipeline, args=(record,), daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "id": record.get("id")}).encode())

    def log_message(self, format, *args):
        print(f"[http] {args[0]} {args[1]}")

if __name__ == "__main__":
    print(f"[worker] Listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
