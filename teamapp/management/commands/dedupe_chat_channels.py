"""
Heal duplicate default chat channels.

Symptom: tenants see triplicate (or more) copies of #general,
#operations, #support in the Team Chat sidebar. Caused by race
conditions in `ensure_tenant_default_channels` — the per-name
`.filter().first()` check has no transactional protection, so two
near-simultaneous Store.post_save signals (e.g. when a tenant adds
multiple stores in a row) can both pass the existence check before
either has committed, then each calls .create() and we end up with
duplicate ChatChannel rows for the same (owner, name, is_dm=False).

This command:
  1. Groups non-DM channels by (owner_id, name).
  2. Picks the canonical row (oldest by id).
  3. Re-parents every ChatMessage, ChannelMember, and
     ChatReadReceipt from the duplicates onto the canonical row.
  4. Deletes the duplicate ChatChannel rows.

Idempotent — safe to run on every deploy. After the migration that
adds the unique constraint, future dupes are impossible.
"""
from collections import defaultdict
from django.core.management.base import BaseCommand
from django.db import transaction
from teamapp.models import ChatChannel, ChannelMember, ChatMessage, ChatReadReceipt


class Command(BaseCommand):
    help = "Merge duplicate (owner, name) non-DM chat channels into one canonical row."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report duplicates without deleting them.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]

        # Group all non-DM channels by (owner_id, name).
        groups: dict[tuple, list[ChatChannel]] = defaultdict(list)
        for ch in ChatChannel.objects.filter(is_dm=False).order_by("id"):
            groups[(ch.owner_id, ch.name)].append(ch)

        dupes_found = 0
        dupes_merged = 0

        for (owner_id, name), channels in groups.items():
            if len(channels) < 2:
                continue
            dupes_found += len(channels) - 1
            canonical = channels[0]
            duplicates = channels[1:]

            self.stdout.write(self.style.WARNING(
                f"  • owner={owner_id} #{name}: {len(channels)} copies "
                f"(keeping id={canonical.id}, merging "
                f"{[d.id for d in duplicates]})"
            ))

            if dry_run:
                continue

            with transaction.atomic():
                for dup in duplicates:
                    # Re-parent messages.
                    ChatMessage.objects.filter(channel=dup).update(channel=canonical)
                    # Re-parent or merge memberships (avoid IntegrityError on
                    # (channel, user) unique pair by checking first).
                    for m in ChannelMember.objects.filter(channel=dup):
                        existing = ChannelMember.objects.filter(
                            channel=canonical, user=m.user,
                        ).first()
                        if existing:
                            # Keep the active flag if EITHER row is active.
                            if m.is_active and not existing.is_active:
                                existing.is_active = True
                                existing.save(update_fields=["is_active"])
                            m.delete()
                        else:
                            m.channel = canonical
                            m.save(update_fields=["channel"])
                    # Re-parent or merge read receipts similarly.
                    for r in ChatReadReceipt.objects.filter(channel=dup):
                        existing = ChatReadReceipt.objects.filter(
                            channel=canonical, user=r.user,
                        ).first()
                        if existing:
                            # Keep the LATER last_read_at so we don't
                            # accidentally mark old messages as unread.
                            if r.last_read_at and (
                                not existing.last_read_at
                                or r.last_read_at > existing.last_read_at
                            ):
                                existing.last_read_at = r.last_read_at
                                existing.save(update_fields=["last_read_at"])
                            r.delete()
                        else:
                            r.channel = canonical
                            r.save(update_fields=["channel"])
                    # Finally delete the duplicate channel.
                    dup.delete()
                    dupes_merged += 1

        if dupes_found == 0:
            self.stdout.write(self.style.SUCCESS(
                "chat channels: no duplicates found ✓"
            ))
        elif dry_run:
            self.stdout.write(self.style.WARNING(
                f"chat channels (dry-run): {dupes_found} duplicate(s) found. "
                f"Re-run without --dry-run to merge."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"chat channels: merged {dupes_merged}/{dupes_found} duplicate(s) ✓"
            ))
