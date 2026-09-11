# Deploy លើ Render — Kairozen SMM Sell Bot

## 1. រៀបចំ Repo
ដាក់ file ទាំងនេះនៅ root ដដែលគ្នា (GitHub repo):
- `sell_smm_bot.py`
- `kairozen_shop_bot.py`  ← ចាំបាច់ស្ថិតនៅជាមួយគ្នា (ត្រូវ subprocess spawn)
- `requirements.txt`
- `render.yaml`
- `Procfile` (fallback បើមិនប្រើ render.yaml)

## 2. បង្កើត Service
Render Dashboard → **New +** → **Blueprint** → ភ្ជាប់ repo នេះ → Render នឹងអាន `render.yaml` ដោយស្វ័យប្រវត្តិ
(ឬបើមិនប្រើ Blueprint: **New +** → **Background Worker**, ជ្រើស repo, Build: `pip install -r requirements.txt`, Start: `python sell_smm_bot.py`)

⚠️ Bot ប្រើ `infinity_polling()` — **មិនមែន** web server ទេ → ត្រូវជ្រើស **Background Worker**
មិនមែន **Web Service** (Web Service នឹងបរាជ័យព្រោះមិន bind port)។ Worker តម្រូវ paid plan (Starter ឡើងទៅ គ្មាន free tier)។

## 3. Environment Variables (Render → Environment tab)
ចាំបាច់:
- `BOT_TOKEN` — Token @BotFather របស់ sell bot
- `ADMIN_ID` — Telegram ID របស់អ្នក
- `PANEL_URL` — URL SMM panel
- `BAKONG_ID` — គណនី Bakong ដែលទទួលលុយ $0.75 (ឧ. `name@aclb`)

ស្រេចចិត្ត (មាន default រួចហើយក្នុង render.yaml):
- `BAKONG_NAME`, `BAKONG_CITY`, `BAKONG_CURRENCY`, `PRICE_USD`
- `BAKONG_TOKEN` — Bakong Developer Token (https://api-bakong.nbc.gov.kh/register/) → បើដាក់ បង់ប្រាក់ verify ស្វ័យប្រវត្តិ

## 4. Persistent Disk (សំខាន់!)
Render redeploy = disk ធម្មតាត្រូវលុប។ `render.yaml` ភ្ជាប់ disk 1GB ទៅ `/var/data`,
ហើយកំណត់ `INSTANCES_DIR=/var/data/instances` និង `DB_FILE=/var/data/sell_bot_db.json`
ដើម្បីកុំឲ្យបាត់ order/buyer/instance token ពេល redeploy ។ កុំកែ 2 env នេះ លុះត្រាតែដឹងច្បាស់។

## 5. Deploy & Verify
- ចុច **Deploy** → មើល Logs ត្រូវឃើញ `Sell bot $0.75 script=...`
- បើឃើញ `KHQR lib: ❌` ក្នុង Setup របស់ shop bot instance → `bakong-khqr[image]` មិនទាន់ install ត្រឹមត្រូវ, ពិនិត្យ build log
- សាកល្បង `/start` → ទិញ → ត្រូវឃើញរូប QR (មិនមែនតែ Demo button)
