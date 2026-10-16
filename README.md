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
├── docker-compose.yml         # Root compose file for 1-command launch
└── HANDOVER_GUIDE.md          # Complete step-by-step deployer & operator handover manual
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

To run the full 29-test verification suite:
```bash
python run_tests.py
```

For complete deployment and customization instructions, see [`HANDOVER_GUIDE.md`](HANDOVER_GUIDE.md).
