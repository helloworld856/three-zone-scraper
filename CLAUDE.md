# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A PyQt5 desktop tool station for centralized web scraping across YouTube, TikTok, X/Twitter, and Instagram, plus AIGC title classification and XLSX merging. Requires Python 3.10+.

## Commands

```bash
# Install dependencies and Playwright browser
pip install -r requirements.txt
python -m playwright install chromium

# Launch the desktop tool station
python main.py

# Run a single tool directly (bypassing the launcher)
python -m src.studio.tool_runner --tool-id <tool_id>

# List available tool IDs
python -m src.studio.tool_runner --tool-id <tool_id> --check

# Lint
ruff check .

# Run UI logic tests (also run by CI)
python test/test_visibility.py

# Run pause state machine tests (requires pytest: pip install pytest)
python -m pytest test/test_pause_state_machine.py -v

# Run TikTok profile scraping tests
python test/test_tiktok_profile.py
```

### All tool IDs (from `src/studio/registry.py`)

**YouTube:** `youtube_keyword_mining`, `youtube_channel_profiles`, `youtube_paired_context_metrics`, `youtube_channel_works`, `youtube_top_comments`

**TikTok:** `tiktok_keyword_metrics`, `tiktok_profile_directory`, `tiktok_profile_videos`, `tiktok_paired_context_metrics`, `tiktok_top_comments`

**X/Twitter:** `x_keyword_video_search`, `x_tweet_author_profiles`, `x_paired_context_metrics`, `x_tweet_metrics`, `x_profile_tweets`, `x_top_comments`

**Instagram:** `instagram_profile_works`

**数据处理:** `judge_aigc`, `xlsx_merge`

## Environment

Copy `.env` and set your credentials. The AIGC judge reads `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL_NAME` (the config also falls back to unprefixed `API_KEY`, `BASE_URL`, `MODEL_NAME`). YouTube tools need a Google API key entered in the UI (`api_key` field).

Ruff config is in `pyproject.toml` (line-length 150, ignores E402/F841). There is no build-system metadata — the project is run directly, not pip-installed.

## Architecture

### Two-layer process model

The main window (`src/studio/qt_app.py`) launches individual tool windows as **separate QProcess** instances via `tool_runner.py`. Each tool gets its own Python process to avoid blocking the launcher. Tools communicate results via `log_callback` + `finish_callback` signals.

YouTube tools require a **Google API key** (field `api_key`). TikTok, X/Twitter, and Instagram tools connect to a **local Chrome via CDP** (port 9222 by default). The project auto-launches Chrome with `--remote-debugging-port=9222` and persists login state in `<workspace_root>/user_data/`.

### Platform scraping strategies

- **YouTube**: Uses `googleapiclient` (YouTube Data API v3). `src/platforms/youtube/context.py` contains the channel resolution, upload playlist pagination, and video detail fetching logic shared across YouTube tools. Channel works (`youtube_channel_works`) adds a Playwright fallback for Posts scraping.
- **TikTok**: Uses Playwright CDP (`connect_existing_chromium`). Two-tier approach: first tries TikTok's internal API (`/api/post/item_list/`) via `page.evaluate` fetch with `secUid`; if the API doesn't find the target video, falls back to scrolling the user's profile grid (`fallback_rows_from_profile`).
- **X/Twitter**: Uses Playwright CDP. Three-tier approach: first tries author search (`from:<handle> since:... until:...`), then profile timeline scrolling (`/with_replies`, `/media` variants), finally opens the target tweet page to reverse-lookup the author profile and re-scan.
- **Instagram**: Uses Playwright CDP. Scrolls the profile grid to collect work links, then opens each work in a detail page to extract publish time, text content, and engagement metrics.
### Shared core (`src/core/`)

| Module | Purpose |
|---|---|
| `app_logging.py` | `setup_console_logging` + `get_logger` under `crawler_tool` root logger; called once, idempotent |
| `browser.py` | Chrome CDP connection: find executable, launch with `--remote-debugging-port`, connect Playwright; `ensure_chrome_for_cdp` auto-launches if no browser is detected; `atexit` cleanup kills the launched Chrome |
| `output.py` | `get_workspace_root()` (finds repo root via `main.py`/`requirements.txt`), `build_output_path(platform, filename)` creates dated output paths under `output/<platform>/` |
| `xlsx.py` | `XlsxRowWriter` — incremental XLSX writer with temp-file atomic saves via `os.replace`; `MultiSheetXlsxWriter` for multi-sheet output |
| `csv_utils.py` | `sanitize_csv_cell` — strips newlines from cell values for CSV/XLSX compatibility |
| `number_format.py` | `expand_compact_number` — converts "1.2K" → "1200", handles CJK units (万/亿) |
| `timing.py` | `should_stop`, `interruptible_sleep`, `random_cooldown`, `wait_if_paused` — cooperative stop/pause-event checks that every scraper must call in its inner loops |

`src/studio/base.py` defines `ToolSpec` (dataclass: `tool_id`, `name`, `category`, `summary`, `entrypoint`, `implementation_path`, `tags`) and `load_object(dotted_path)` which imports and returns a class/factory from a dotted string path. Both are used by `registry.py` and `tool_runner.py`.

### UI layer (`src/ui/base.py` + `src/ui/config_dialog.py`)

`SimpleToolWindow(QWidget)` is the base for all tool windows. Subclasses define a list of `FieldSpec` and implement `run_task(values, log_callback, finish_callback, stop_event, pause_event)`. The base handles: Start/Pause/Continue/Stop buttons with a 3-state machine (idle → running ↔ paused), a per-tool config dialog ("参数配置" button), worker thread management, log display, and stop/pause-event propagation.

**FieldSpec kinds:** `text` (QLineEdit), `multiline` (QPlainTextEdit), `int` (QSpinBox), `combo` (QComboBox), `file`/`folder` (line edit + browse button), and `text_or_file` — a composite widget that lets the user either paste text directly or select a TXT file. Fields can be conditionally shown/hidden via `bind_field_visibility(trigger_field, trigger_value, target_fields)`.

**ConfigParam system** (`src/ui/config_dialog.py`): Each tool window can expose tunable parameters via `tool_config_params()` → returns a list of `ConfigParam(key, label, kind, default, ...)`. The `ConfigDialog` renders these as a scrollable form (int/float/combo/bool). Saved values persist in `self.config_values` and are merged into `values` by `_run_worker` before `run_task` is called.

**Worker threading model:** `run_task` executes in a plain `threading.Thread` (daemon=False). UI updates are thread-safe via `WorkerSignals(QObject)` — three pyqtSignals: `log` → `append_log`, `finished` → `_finish_success`, `failed` → `_finish_error`. Scrapers call `log_callback(str)` for progress and `finish_callback(output_path)` on completion.

### Tool registry pattern

All tools are declared in `src/studio/registry.py` as `ToolSpec` dataclasses with a unique `tool_id`, `category` (YouTube/TikTok/X-Twitter/Instagram/数据处理), `entrypoint` (dotted path to a QWidget class or factory), `implementation_path`, and `tags`. The launcher uses these for search, filtering, and QProcess instantiation.

### AIGC judge (`src/judge_aigc/`)

Two-stage classification: local keyword/Unicode-range detection runs first (fast, free); unresolved titles are sent to DeepSeek via LangChain LangGraph for final determination. Configured via `.env`. Batch processing with incremental XLSX saves and existing-row deduplication.

## Key patterns

- All scraper entry functions (`run_*_spider`, `run_scraper`) follow the same signature: `(required_params, ..., log_callback, finish_callback, stop_event, pause_event=None)`. They call `finish_callback(output_path)` on completion.
- Scrapers must cooperatively check `should_stop(stop_event)` and `wait_if_paused(pause_event, stop_event)` in their inner loops. Use `interruptible_sleep` and `random_cooldown` from `src/core/timing.py` instead of plain `time.sleep`.
- Output goes to `output/<platform>/` with date-stamped filenames. Paths are built via `build_output_path(platform, filename)`.
- Input TXT files use tab-separated pairs (`video_url\tprofile_url`) or one URL per line, with `#` comment lines supported.
- Chrome user data persists in `<workspace_root>/user_data/` — first login persists across sessions.
- `main.py` is a 3-line shim that delegates entirely to `src.studio.qt_app.main()`.
- Config propagation: `SimpleToolWindow._run_worker` merges `self.config_values` (from the per-tool ConfigDialog) into `values` before calling `run_task`. Tool windows filter config keys relevant to their scraper (e.g., `{k: v for k, v in values.items() if k.startswith("youtube_")}`) and pass them as a `config` dict.
- `text_or_file` fields: the window base class provides `_text_to_tempfile(text)` which writes inline text to a temp file under `output/temp/` so scrapers always receive a file path regardless of input mode.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and PR to main/master. On `windows-latest` with Python 3.10: installs deps, runs `ruff check .`, then runs `python test/test_visibility.py`.
