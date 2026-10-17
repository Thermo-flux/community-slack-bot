# 🤖 Community Champion Slack Bot

A lightweight, production-grade Slack bot designed to track community activity, award contribution points (XP), calculate ranks & levels with progress bars, celebrate accomplishments, and chat intelligently with users via AI.

Runs effortlessly on a **small 2GB 2 vCPU server** or **free cloud service** (<70MB RAM footprint).

---

## 📁 Organized Project Structure

```text
community_slack_bot/
├── config/
│   ├── config.json            # Deployer settings: XP rates, emojis, level tiers, templates
│   ├── soul.md                # Bot persona, tone, character rules, AI system prompt
│   └── slack_manifest.json    # 1-click Slack App setup manifest
├── src/
│   ├── bot.py                 # Main bot launcher (Socket Mode)
│   ├── config.py              # Configuration and .env loader
│   ├── database.py            # SQLite thread-safe database manager
│   ├── leveling.py            # Level calculations, XP evaluation, progress bars
│   ├── ai_service.py          # Multi-provider AI reasoning engine (Gemini, OpenAI, Anthropic, Mock)
│   └── handlers.py            # Slack message tracking, @mentions, and slash commands
├── deployment/
│   ├── Dockerfile             # Production container definition
│   ├── docker-compose.yml     # 1-command Docker deployment with persistent data volume
│   └── slackbot.service       # Linux systemd background service file
├── tests/
│   ├── test_database.py       # Database unit & concurrency tests
│   ├── test_leveling.py       # XP math, rank cards, and progress bar tests
│   ├── test_ai_service.py     # AI provider & offline fallback tests
│   ├── test_handlers.py       # Bot loop prevention & command routing tests
│   └── test_stress.py         # High-concurrency stress tests (25 threads, 500 messages)
├── data/                      # Persistent SQLite storage directory
├── run_bot.py                 # Convenient root-level launcher script
├── run_tests.py               # Convenient root-level test runner
├── .env.example               # Environment credential template with instructions
├── requirements.txt           # Pinned lightweight dependencies
├── pytest.ini                 # Test runner configuration
├── Dockerfile                 # Root container definition for Cloud platforms
└── docker-compose.yml         # Root compose file for 1-command launch
```

---

## 🚀 Quick Start (3 Steps)

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
2. **Setup Credentials:**
   ```bash
   cp .env.example .env
   # Edit .env with your SLACK_BOT_TOKEN and SLACK_APP_TOKEN
   ```
3. **Launch the Bot:**
   ```bash
   python run_bot.py
   ```

To run the verification suite:
```bash
python run_tests.py
```

## AI replies

Direct mentions, direct messages, recognized Goose name calls, and explicit untagged opinion questions naming Goose or Orry return quickly to the Slack listener and run in a bounded worker queue. Goose Lee posts a short status message, then replaces it with the answer. If the queue is full or the provider is unavailable, the bot replies with a brief fallback.

The Grok 4.7 configuration uses low reasoning effort. AI_TIMEOUT_SECONDS sets the provider connection/read timeout and the response-body read deadline (15 seconds by default); provider requests are not retried. AI_MAX_REPLY_CHARS defaults to 400. AI_REPLY_WORKERS and AI_REPLY_QUEUE_SIZE control parallel replies and queued work.

Goose Lee uses the 10 most recent messages from the same conversation for an unthreaded request, or all messages in the relevant thread. It fetches context only after a call trigger and redacts common credential formats before sending it to the model. Ordinary channel messages do not trigger an AI reply or enter the model context. Replies stay to one or two short sentences and usually have no emoji. Any emoji is limited to one approved Slack shortcode.

The Slack manifest enables one-to-one DMs with `message.im` and `im:history`. Reinstall the app after applying manifest scope changes.

Channel replies are enabled only in the configured public `#loadout` and `#loadout-development` channels after Slack confirms each ID resolves to its exact channel name at startup. Set `OFFICIAL_LOADOUT_CHANNEL_ID` or `LOADOUT_DEVELOPMENT_CHANNEL_ID` to override an ID for another workspace. Setting `WATCHED_CHANNELS` to a comma-separated list of channel IDs overrides the pair and watches exactly those channels (public or private). Direct messages remain enabled.

For deployment, see `deployment/`, `Dockerfile`, and `docker-compose.yml`. Copy `.env.example` and set the Slack tokens and channel IDs before starting the bot.
