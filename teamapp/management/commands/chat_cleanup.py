"""
Production data-cleanup for Team Chat after the tenant-scoping refactor.

Safe to run multiple times. Run with --dry-run first to preview.

Steps:
  1. Collapse duplicate DM channels. A duplicate is identified by the
     canonical DM slug pattern ``dm-<low_id>-<high_id>`` — that pattern
     encodes the intended participant pair, regardless of how broken the
     participants M2M ended up after race-condition inserts. For each
     duplicate group we keep the row with the most messages (tie-break: max
     id) and delete the rest. Messages cascade-delete.
  2. Delete empty / orphan DM channels (no messages AND <2 participants).
  3. Verify no NULL-owner non-DM channels remain. Logs an error if any do —
     means migration 0016 hasn't been applied yet.

Usage:
  ./venv/bin/python manage.py chat_cleanup --dry-run
  ./venv/bin/python manage.py chat_cleanup
"""
import re
from collections import defaultdict
from django.core.management.base import BaseCommand
from django.db import transaction
from teamapp.models import ChatChannel

# Canonical DM slug: dm-<low>-<high>. We use this as the canonical key so
# that DMs with a half-populated participants M2M still collapse to one row.
_DM_SLUG_RE = re.compile(r"^dm-(\d+)-(\d+)$")


def _canonical_key(channel):
    m = _DM_SLUG_RE.match(channel.slug or "")
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return ("slug", tuple(sorted([a, b])))
    pids = tuple(sorted(channel.participants.values_list("id", flat=True)))
    if len(pids) == 2:
        return ("slug", pids)
    return ("uniq", channel.id)  # un-collapsible — leave it alone


class Command(BaseCommand):
    help = "Clean up duplicate/empty Team Chat channels after tenant-scoping migration."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change, but don't write anything.")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        prefix = "[DRY RUN] " if dry else ""

        # ── 1. Duplicate DMs ────────────────────────────────────────────────
        groups = defaultdict(list)
        for ch in ChatChannel.objects.filter(is_dm=True).prefetch_related("participants"):
            groups[_canonical_key(ch)].append(ch)

        dup_groups = {k: chs for k, chs in groups.items() if k[0] == "slug" and len(chs) > 1}
        self.stdout.write(f"{prefix}Found {len(dup_groups)} duplicate-DM group(s).")

        total_deleted_dups = 0
        for key, chs in dup_groups.items():
            ranked = sorted(chs, key=lambda c: (c.messages.count(), c.id), reverse=True)
            keep = ranked[0]
            losers = ranked[1:]
            self.stdout.write(
                f"  canonical={key[1]} → keep id={keep.id} slug={keep.slug!r} "
                f"(msgs={keep.messages.count()}); delete ids={[(c.id, c.slug) for c in losers]}"
            )
            if not dry:
                with transaction.atomic():
                    for c in losers:
                        # Merge participants into the keeper just in case
                        # they didn't fully overlap.
                        keep.participants.add(*list(c.participants.all()))
                        c.delete()
            total_deleted_dups += len(losers)
        self.stdout.write(f"{prefix}Deleted {total_deleted_dups} duplicate DM channel(s).")

        # ── 2. Empty / orphan DMs (no messages AND fewer than 2 participants) ─
        empty_qs = []
        for ch in ChatChannel.objects.filter(is_dm=True).prefetch_related("participants"):
            if ch.messages.exists():
                continue
            if ch.participants.count() < 2:
                empty_qs.append(ch)
        self.stdout.write(f"{prefix}Found {len(empty_qs)} empty/orphan DM channel(s).")
        for ch in empty_qs:
            self.stdout.write(
                f"  delete id={ch.id} slug={ch.slug!r} participants={list(ch.participants.values_list('id', flat=True))}"
            )
        if not dry:
            for ch in empty_qs:
                ch.delete()

        # ── 3. Sanity: no NULL-owner non-DM channels left ───────────────────
        null_owner_count = ChatChannel.objects.filter(owner__isnull=True, is_dm=False).count()
        if null_owner_count:
            self.stdout.write(self.style.ERROR(
                f"FATAL: {null_owner_count} non-DM channel(s) still have NULL owner."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("OK: no NULL-owner non-DM channels."))
