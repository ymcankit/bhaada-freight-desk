# Bhaada on real WhatsApp

`whatsapp_bot.py` puts the same engine behind a real WhatsApp number (Meta WhatsApp Cloud API, free test number).
The broker sends a load as text or a voice note and gets the quote back with **Approve / Reject** buttons;
after approval an **Owner backs out** button shows the agent re-booking on its own.
Owner replies are still simulated personas: only the broker side is live WhatsApp.

## 1. Meta app and test number (about 15 minutes, no business verification)
1. developers.facebook.com → **My Apps → Create app** → use case **Other** → type **Business** → name it `Bhaada`.
2. In the app, add the **WhatsApp** product → **API Setup**.
3. Copy the **Phone number ID** of the test number, and click **Generate access token** (valid 24 hours).
4. Under **To**, add your phone and up to 4 teammates' phones; each confirms with the code WhatsApp sends.

## 2. Run the webhook
Put the values in `.streamlit/secrets.toml` (see the example file) or as environment variables:
`WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_VERIFY_TOKEN`, `GROQ_API_KEY`.

**Option A, for recording the demo (local):**

    .venv/bin/uvicorn whatsapp_bot:app --port 8000
    cloudflared tunnel --url http://localhost:8000       # prints https://<random>.trycloudflare.com

**Option B, always on (Render free tier):** render.com → New → **Web Service** → this GitHub repo →
Build `pip install -r requirements.txt` → Start `uvicorn whatsapp_bot:app --host 0.0.0.0 --port $PORT` →
add the four variables under **Environment**. The free tier sleeps after 15 idle minutes, so open the URL once before a demo.

## 3. Connect Meta to the webhook
WhatsApp → **Configuration → Webhook → Edit**: Callback URL `https://<your host>/webhook`,
Verify token = your `WHATSAPP_VERIFY_TOKEN` → **Verify and save** → under Webhook fields, **Subscribe** to `messages`.

## 4. Try it
From a registered phone, send `hi` to the test number, then e.g. *Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL*
(or a voice note). Send `new` at any time to start over.

## Limits to say out loud
- A test number can only talk to the up-to-5 phones you registered, so judges use the Streamlit link; show WhatsApp in the video.
- The temporary token expires after 24 hours. For a lasting one: Business Settings → **System users** → generate a token
  with `whatsapp_business_messaging` and `whatsapp_business_management`.
- Messaging owners for real needs approved templates and business verification: that is the production step.
