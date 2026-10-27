# Bhaada on real WhatsApp

`server.py` runs the console, its API and the WhatsApp Cloud API webhook on one live desk. Brokers send loads to the
desk's WhatsApp number; fleet owners register on the same number, answer the morning check-in and negotiate there.

## 1. Meta app and test number (about 15 minutes, no business verification)
1. developers.facebook.com → **My Apps → Create app** → use case **Other** → type **Business** → name it `Bhaada`.
2. Add the **WhatsApp** product → **API Setup**. Copy the **Phone number ID** and click **Generate access token** (24 hours).
3. Under **To**, add up to 5 phones: one broker and up to four fleet owners (teammates). Each confirms with a code.

## 2. Run the server
Local: put `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_VERIFY_TOKEN` and `GROQ_API_KEY` in `.streamlit/secrets.toml`, then

    .venv/bin/uvicorn server:app --port 8000
    cloudflared tunnel --url http://localhost:8000      # prints https://<random>.trycloudflare.com

Always on: render.com → **New → Blueprint** → this repo (it reads `render.yaml`) → fill in the four secret values.
The free plan sleeps after 15 idle minutes and forgets registered owners on a restart: open the link once before a demo.

## 3. Connect Meta to the webhook
WhatsApp → **Configuration → Webhook → Edit**: Callback URL `https://<your host>/webhook`, Verify token = your
`WHATSAPP_VERIFY_TOKEN` → **Verify and save** → **Subscribe** to `messages`.

## 4. Run a real load
1. Each owner phone sends: `owner Sharma Transport, Bhiwandi`.
2. In the console, switch to **Live owners** → **Owners and pool** → **Send morning check-in**.
   Owners reply, e.g. `32 ft MXL, Bhiwandi, aaj, ₹38/km`.
3. The broker phone sends a load (text or voice note): *Kal subah Bhiwandi se Surat, 14 ton, 32 MXL*.
4. Owners get the load and reply with a rate; the agent counters inside the ceiling. When the round closes, the broker
   gets the quote with **Approve / Reject**; on approval the chosen owner is asked to hold (*HAAN*).
5. If that owner later replies *cancel*, the desk re-confirms the backups and re-books at the shipper's fixed price.

## Limits to say out loud
- A test number talks only to the phones you registered; judges use the console link (Sandbox owners).
- Messaging owners who haven't written in 24 hours needs approved templates; a production number needs business verification.
