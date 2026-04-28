# Azure Pipeline Setup — No CLI Required

## Overview

The pipeline runs as a small Python worker on Railway (free tier). It:
1. Receives a webhook from Supabase when you add a YouTube URL
2. Downloads the audio via yt-dlp
3. Uploads to Azure Blob Storage
4. Transcribes with Azure Speech (speaker diarization)
5. Generates summary, sentiment, notes, and production date estimate via Azure OpenAI
6. Saves everything back to your Supabase database

---

## Step 1: Create Azure Blob Container

1. Go to [portal.azure.com](https://portal.azure.com)
2. Open your `wtptranscriptionstorage` storage account
3. Click **Containers** → **+ Container**
4. Name it `transcriptions`, access level **Private**
5. Click **Create**

---

## Step 2: Deploy the Worker to Railway

1. Go to [railway.app](https://railway.app) and sign up (free)
2. Click **New Project** → **Deploy from GitHub repo**
3. Connect your GitHub account and select `insightful-tube-explorer`
4. When asked for the root directory, set it to **`worker`**
5. Railway will auto-detect Python and build with nixpacks (includes yt-dlp + ffmpeg)
6. Add these **Environment Variables** in Railway dashboard → your service → Variables:

```
SUPABASE_URL=https://wrsrjnqolfytzwbgbkgb.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<your service role key — find in Supabase > Settings > API>
AZURE_SPEECH_ENDPOINT=https://eastus.api.cognitive.microsoft.com/
AZURE_SPEECH_API_KEY=<YOUR_AZURE_SPEECH_API_KEY>
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=wtptranscriptionstorage;AccountKey=<YOUR_AZURE_STORAGE_ACCOUNT_KEY>;EndpointSuffix=core.windows.net
AZURE_OPENAI_API_KEY=<YOUR_AZURE_OPENAI_API_KEY>
AZURE_OPENAI_DEPLOYMENT=gpt-5-mini
```

7. After deploying, copy your Railway service URL (looks like `https://your-app.up.railway.app`)

---

## Step 3: Set up the Supabase Database Webhook

1. Go to your [Supabase dashboard](https://supabase.com/dashboard/project/wrsrjnqolfytzwbgbkgb)
2. Left sidebar → **Database** → **Webhooks**
3. Click **Create a new hook**
4. Fill in:
   - **Name:** `trigger-process-analysis`
   - **Table:** `analyses`
   - **Events:** ✅ Insert
   - **Type:** HTTP Request
   - **URL:** `https://your-app.up.railway.app` ← paste your Railway URL here
   - **HTTP Headers:** add one header:
     - Key: `Content-Type`  Value: `application/json`
5. Click **Create webhook**

That's it! Now whenever you paste a YouTube URL in the app, it'll automatically kick off the full pipeline.

---

## How to find your Supabase Service Role Key

1. Go to [Supabase dashboard](https://supabase.com/dashboard/project/wrsrjnqolfytzwbgbkgb)
2. Left sidebar → **Settings** → **API**
3. Copy the **service_role** key (not the anon key)

