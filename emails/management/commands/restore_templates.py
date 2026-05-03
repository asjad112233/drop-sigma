from django.core.management.base import BaseCommand
from emails.default_templates import PORTAL_DEFAULT_TEMPLATES
from emails.models import EmailTemplate
from stores.models import Store

COPY_FIELDS = [
    'name', 'category', 'status', 'subject', 'preheader', 'body_html',
    'trigger_type', 'trigger_delay_minutes', 'working_hours_only',
    'throttle_per_day', 'is_category_default', 'is_global',
    'description', 'tags', 'from_email', 'sender_name', 'reply_to',
    'cc_emails', 'bcc_emails', 'use_default_signature', 'custom_signature', 'footer',
]


class Command(BaseCommand):
    help = 'Restore portal default templates for all stores (or a specific store)'

    def add_arguments(self, parser):
        parser.add_argument('--store-id', type=int, help='Restore only for this store ID')
        parser.add_argument('--force', action='store_true', help='Delete existing templates before restoring')

    def handle(self, *args, **options):
        store_id = options.get('store_id')
        force = options.get('force', False)

        stores = Store.objects.filter(id=store_id) if store_id else Store.objects.all()

        for store in stores:
            if force:
                deleted, _ = EmailTemplate.objects.filter(store=store).delete()
                self.stdout.write(f'  Deleted {deleted} templates for {store.name}')

            bulk = []
            for tpl in PORTAL_DEFAULT_TEMPLATES:
                name = tpl.get('name')
                cat = tpl.get('category')
                if not force and EmailTemplate.objects.filter(store=store, name=name, category=cat).exists():
                    continue
                bulk.append(EmailTemplate(store=store, **{k: tpl.get(k) for k in COPY_FIELDS}))

            if bulk:
                EmailTemplate.objects.bulk_create(bulk)
                self.stdout.write(self.style.SUCCESS(f'  Restored {len(bulk)} templates for {store.name}'))
            else:
                self.stdout.write(f'  {store.name}: already up to date (use --force to reset)')

        self.stdout.write(self.style.SUCCESS('Done.'))
