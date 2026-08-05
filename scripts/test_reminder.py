"""
test_reminder.py — Manual test runner for the email reminder follow-up system.

Runs the exact same logic as the `send_reminder_followups` Azure Function timer
trigger so you can test end-to-end without deploying to Azure.

Usage
-----
    # 1. List all active (awaiting_reply) conversations
    python test_reminder.py --list

    # 2. Dry-run: show which conversations WOULD get a reminder (no emails sent)
    python test_reminder.py --dry-run

    # 3. Actually send reminder emails to all stale conversations
    python test_reminder.py --send

    # 4. Force-send a reminder to ONE specific conversation (ignores time threshold)
    python test_reminder.py --send --conv-id <conversation_id>

    # 5. Override the stale threshold (default: FOLLOWUP_REMINDER_INTERVAL_MINUTES from .env)
    python test_reminder.py --send --minutes 1

Options
-------
    --list        Print every active conversation and its current state.
    --dry-run     Show which conversations would be reminded, without sending.
    --send        Send reminder emails to all stale conversations (or --conv-id).
    --conv-id ID  Target a single conversation by its conversation_id.
    --minutes N   Override the staleness threshold to N minutes.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

# ── Load .env so Config picks up credentials ─────────────────────────────────
from dotenv import load_dotenv
load_dotenv(override=True)

from src.config import Config
from src.services.conversation_tracker import ConversationTracker
from src.services.followup_orchestrator import FollowupOrchestrator


# ── Colour helpers (works on Windows ≥ 10 terminals) ─────────────────────────
def _green(s):  return f"\033[32m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"
def _red(s):    return f"\033[31m{s}\033[0m"
def _bold(s):   return f"\033[1m{s}\033[0m"
def _dim(s):    return f"\033[2m{s}\033[0m"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _age_str(ts: str | None) -> str:
    """Return human-readable age of a timestamp, e.g. '3 min ago'."""
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        total_sec = int(delta.total_seconds())
        if total_sec < 60:
            return f"{total_sec}s ago"
        if total_sec < 3600:
            return f"{total_sec // 60}m ago"
        return f"{total_sec // 3600}h {(total_sec % 3600) // 60}m ago"
    except Exception:
        return ts[:19]


def _print_state(state, *, highlight: bool = False) -> None:
    conv_short = state.conversation_id[:28]
    last_act   = state.last_followup_at or state.created_at
    age        = _age_str(last_act)
    pct        = state.followup_count / max(state.max_followups, 1)
    bar        = ("█" * int(pct * 10)).ljust(10)
    label      = _bold(conv_short) if highlight else conv_short
    print(
        f"  {label}\n"
        f"    sender   : {state.sender_email}\n"
        f"    subject  : {state.subject[:60]}\n"
        f"    status   : {state.status}  |  "
        f"follow-ups: {state.followup_count}/{state.max_followups}  [{bar}]\n"
        f"    customer : {state.customer_id or '—'}  |  "
        f"last activity: {age}\n"
        f"    missing  : {', '.join(state.missing_fields) or 'none'}"
    )


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(tracker: ConversationTracker) -> None:
    active = tracker.list_active()
    print(_bold(f"\nActive conversations: {len(active)}"))
    if not active:
        print("  (none)")
        return
    for s in active:
        _print_state(s)
        print()


def cmd_dry_run(tracker: ConversationTracker, threshold_min: float) -> None:
    threshold_h = threshold_min / 60.0
    stale = tracker.list_stale(threshold_hours=threshold_h)
    print(_bold(f"\nStale conversations (>{threshold_min:.1f} min since last follow-up): {len(stale)}"))
    if not stale:
        print(_green("  None — no reminders needed right now."))
        return
    for s in stale:
        _print_state(s, highlight=True)
        print()
    print(_yellow(f"  → {len(stale)} reminder(s) would be sent."))


def cmd_send(
    tracker:       ConversationTracker,
    orchestrator:  FollowupOrchestrator,
    threshold_min: float,
    conv_id:       str | None,
) -> None:
    # Build target list
    if conv_id:
        state = tracker.load(conv_id)
        if not state:
            print(_red(f"Conversation not found: {conv_id}"))
            sys.exit(1)
        targets = [state]
        print(_bold(f"\nForce-sending reminder to: {conv_id[:28]}"))
    else:
        threshold_h = threshold_min / 60.0
        targets = tracker.list_stale(threshold_hours=threshold_h)
        print(_bold(f"\nSending reminders to {len(targets)} stale conversation(s) "
                    f"(>{threshold_min:.1f} min)"))

    if not targets:
        print(_green("  Nothing to send."))
        return

    # Build Graph client + token
    try:
        from src.models.graph_models import GraphConfig
        from src.services.graph_client import GraphClient, get_app_token

        graph_cfg = GraphConfig(
            tenant_id     = Config.AZURE_AD_TENANT_ID     or "",
            client_id     = Config.AZURE_AD_CLIENT_ID     or "",
            client_secret = Config.AZURE_AD_CLIENT_SECRET or "",
            redirect_uri  = Config.GRAPH_REDIRECT_URI,
        )
        graph_client = GraphClient(graph_cfg)
        access_token = get_app_token()
        print(_dim("  Graph token acquired.\n"))
    except Exception as exc:
        print(_red(f"  Failed to get Graph token: {exc}"))
        print(_red("  Check AZURE_AD_TENANT_ID / CLIENT_ID / CLIENT_SECRET in .env"))
        sys.exit(1)

    sent = skipped = failed = 0
    for state in targets:
        print(f"  → {state.conversation_id[:28]}  ({state.sender_email})")
        try:
            result = asyncio.run(
                orchestrator.send_timeout_followup(
                    state        = state,
                    graph_client = graph_client,
                    access_token = access_token,
                )
            )
            if result.action == "followup_sent":
                sent += 1
                print(_green(
                    f"     ✓ Reminder #{result.followup_count} sent  "
                    f"({result.followup_count}/{state.max_followups} used)"
                ))
            elif result.action == "max_retries":
                skipped += 1
                print(_yellow(
                    f"     ⚠ Max follow-ups ({state.max_followups}) reached — skipped"
                ))
            else:
                failed += 1
                print(_red(f"     ✗ Unexpected result: {result.action} — {result.message}"))
        except Exception as exc:
            failed += 1
            print(_red(f"     ✗ Error: {exc}"))

    print()
    print(_bold(f"Done — sent: {sent}  skipped: {skipped}  failed: {failed}"))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the Shepherd AI email reminder system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list",    action="store_true", help="List all active conversations")
    group.add_argument("--dry-run", action="store_true", help="Show which conversations would get a reminder")
    group.add_argument("--send",    action="store_true", help="Send reminder emails now")

    parser.add_argument(
        "--conv-id",
        metavar="ID",
        help="Target a specific conversation_id (use with --send)",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=float(Config.FOLLOWUP_REMINDER_INTERVAL_MINUTES),
        help=f"Staleness threshold in minutes (default: {Config.FOLLOWUP_REMINDER_INTERVAL_MINUTES} from .env)",
    )

    args = parser.parse_args()

    tracker      = ConversationTracker()
    orchestrator = FollowupOrchestrator()

    print(_bold("\n=== Shepherd AI — Reminder Follow-up Tester ==="))
    print(_dim(f"  Storage      : {Config.AZURE_STORAGE_CONNECTION_STRING and 'Azure Blob' or 'Local disk'}"))
    print(_dim(f"  Default max  : {Config.FOLLOWUP_DEFAULT_MAX} follow-ups"))
    print(_dim(f"  Threshold    : {args.minutes} min"))
    print(_dim(f"  Per-customer : {Config.FOLLOWUP_MAX_BY_CUSTOMER or '{}  (all use default)'}"))

    if args.list:
        cmd_list(tracker)
    elif args.dry_run:
        cmd_dry_run(tracker, args.minutes)
    elif args.send:
        cmd_send(tracker, orchestrator, args.minutes, args.conv_id)


if __name__ == "__main__":
    main()
