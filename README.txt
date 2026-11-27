EARNWAVE BOT + TELEGRAM MINI APP

Files:
- app.py: new launcher + Telegram Mini App API. Keep beside existing main.py.
- web/index.html, web/styles.css, web/app.js: Mini App frontend.
- main.py: your existing bot/backend source; keep it unchanged for this integration.

RENDER SETUP
1. Upload app.py and the complete web/ folder to the same GitHub repository/directory as main.py.
2. Keep your existing environment variables BOT_TOKEN and DATABASE_URL.
3. Add MINI_APP_URL = https://YOUR-RENDER-SERVICE.onrender.com/app
   (use your actual Render public HTTPS URL; do not include a trailing slash if possible.)
4. Keep PORT as Render provides it. Keep TRON/BEP20 environment variables and TRONGRID_API_KEY if using those features.
5. Change Render Start Command from `python main.py` to `python app.py`.
6. Deploy. The app sets the Telegram menu button and replaces the old reply keyboard with an Open EarnWave Mini App button.
7. Open the bot in Telegram and send /start.

IMPORTANT
- Do not delete or replace your existing main.py.
- Mini App API calls reject requests without valid Telegram initData. Opening the URL in a normal browser will show the Telegram-only notice; that is expected.
- Use a real HTTPS Render URL for MINI_APP_URL.
- This is an initial integration build. Before accepting real deposits/withdrawals at scale, complete the final security and end-to-end review. The Mini App does not connect any rewarded-ad provider; Watch Ads is not included as a fake reward feature.
- Admin identity is inherited from ADMIN_IDS in main.py.
