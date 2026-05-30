# Data migration: backfill per-tenant default channels and remove legacy globals.
#
# Before this migration, ChatChannel had no `owner` FK, so the 3 default group
# channels (#general, #operations, #support) were shared across EVERY tenant —
# a cross-tenant data leak (Tenant A's member list visible to Tenant B).
#
# This migration:
#   1. For every tenant (User who owns at least one Store), create their own
#      #general / #operations / #support channels with owner=<tenant>.
#      Slugs are namespaced as "<username>-<channel_name>" to keep them unique.
#   2. Copy each tenant's team members + vendors into their new channels.
#   3. Delete the legacy global channels (owner IS NULL, is_dm=False). Their
#      messages cascade-delete with the channel. Chat history is utility data;
#      the user has approved a fresh-start cleanup.
#
# DM channels (is_dm=True) are left untouched — they don't have an owner and
# tenant scope is enforced via `participants`.

from django.db import migrations


DEFAULT_CHANNELS = [
    ("general",    "Team-wide chat"),
    ("operations", "Orders & vendor ops"),
    ("support",    "Customer support discussions"),
]


def forwards(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Store = apps.get_model("stores", "Store")
    ChatChannel = apps.get_model("teamapp", "ChatChannel")
    ChannelMember = apps.get_model("teamapp", "ChannelMember")
    TeamMember = apps.get_model("teamapp", "TeamMember")
    Vendor = apps.get_model("vendors", "Vendor")
    VendorInvitation = apps.get_model("vendors", "VendorInvitation")

    from django.db.models import Q

    # 1) Identify all tenants — Users who own at least one Store.
    tenant_ids = list(Store.objects.values_list("user_id", flat=True).distinct())
    tenant_ids = [t for t in tenant_ids if t]

    for tid in tenant_ids:
        try:
            tenant = User.objects.get(pk=tid)
        except User.DoesNotExist:
            continue

        # Collect channel members for this tenant once.
        store_ids = list(Store.objects.filter(user=tenant).values_list("id", flat=True))
        inv_emails = list(
            VendorInvitation.objects.filter(owner=tenant, status="accepted")
            .values_list("email", flat=True)
        )
        vendor_user_ids = list(
            Vendor.objects.filter(
                Q(assigned_store_id__in=store_ids) | Q(email__in=inv_emails),
                user__isnull=False,
            ).values_list("user_id", flat=True).distinct()
        )
        team_user_ids = list(
            TeamMember.objects.filter(owner=tenant, is_active=True, user__isnull=False)
            .values_list("user_id", flat=True).distinct()
        )

        member_user_ids = set([tenant.id]) | set(vendor_user_ids) | set(team_user_ids)

        for name, desc in DEFAULT_CHANNELS:
            base_slug = f"{tenant.username}-{name}".lower()[:50]
            slug = base_slug
            ctr = 1
            # Defensive: pick a unique slug if a colliding row already exists
            # (e.g. another tenant has the same username prefix).
            while ChatChannel.objects.filter(slug=slug).exists():
                # If a channel with this slug already exists AND it's owned by
                # this tenant + same name, reuse it instead of bumping.
                existing = ChatChannel.objects.filter(
                    slug=slug, owner=tenant, name=name, is_dm=False
                ).first()
                if existing:
                    slug = existing.slug
                    break
                slug = f"{base_slug}-{ctr}"[:50]
                ctr += 1

            ch, _created = ChatChannel.objects.get_or_create(
                owner=tenant, name=name, is_dm=False,
                defaults={"slug": slug, "description": desc},
            )

            for uid in member_user_ids:
                ChannelMember.objects.get_or_create(
                    channel=ch, user_id=uid,
                    defaults={"is_active": True},
                )

    # 2) Delete the legacy global channels (owner IS NULL AND is_dm=False).
    legacy_qs = ChatChannel.objects.filter(owner__isnull=True, is_dm=False)
    legacy_count = legacy_qs.count()
    legacy_qs.delete()
    print(f"[teamapp.0016] deleted {legacy_count} legacy global channel(s)")


def backwards(apps, schema_editor):
    # No-op: reversing would recreate legacy global channels, which is the
    # whole bug we just fixed.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("teamapp", "0015_chatchannel_owner"),
        ("stores", "0001_initial"),
        ("vendors", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
