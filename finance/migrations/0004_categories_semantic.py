"""Base-category keys, inherited kinds, and embeddings (pgvector) for categories and transactions.

``VectorExtension`` creates ``vector`` in this database; it is not a trusted extension, so this
needs the superuser -- which is what the services connect as. On a cluster whose init script
already created it (daten images carry pgvector) it is a no-op.

Data steps, in this transaction:

* The flat defaults bank seeded before the taxonomy get their base key
  (:data:`finance.taxonomy.LEGACY_KEYS`); those that map to a leaf move under their root
  (created if missing) when that does not clash with a sibling. Names are kept.
* Every category takes its root's kind, so a subtree is always one kind.
* Every category and transaction is embedded (a static model, ~1 ms a row), so suggestions and
  semantic search work the moment the release is up.
"""

from typing import Any

import django.db.models.deletion
import pgvector.django.vector
from django.db import migrations, models
from pgvector.django import VectorExtension

BATCH = 500


def key_legacy_defaults(apps: Any, schema_editor: Any) -> None:
    from finance.taxonomy import LEGACY_KEYS, NODES, ROOT_OF

    Category = apps.get_model("finance", "Category")
    for org_id in Category.objects.values_list("organization_id", flat=True).distinct():
        legacy = Category.objects.filter(organization_id=org_id, parent__isnull=True, key__isnull=True, name__in=list(LEGACY_KEYS))
        for category in legacy:
            key = LEGACY_KEYS[category.name]
            if Category.objects.filter(organization_id=org_id, key=key).exists():
                continue
            node = NODES[key]
            Category.objects.filter(pk=category.pk).update(key=key, description=category.description or node.description)
        for category in Category.objects.filter(organization_id=org_id, parent__isnull=True, key__in=list(LEGACY_KEYS.values())):
            root_key = ROOT_OF[category.key]
            if root_key == category.key:
                continue  # already a root
            root = Category.objects.filter(organization_id=org_id, key=root_key).first()
            if root is None:
                node = NODES[root_key]
                if Category.objects.filter(organization_id=org_id, parent__isnull=True, name=node.name).exists():
                    continue
                root = Category.objects.create(organization_id=org_id, key=root_key, name=node.name, description=node.description, kind=node.kind, color=node.color)
            if not Category.objects.filter(organization_id=org_id, parent=root, name=category.name).exists():
                Category.objects.filter(pk=category.pk).update(parent=root)


def inherit_root_kinds(apps: Any, schema_editor: Any) -> None:
    Category = apps.get_model("finance", "Category")
    rows = {c.pk: c for c in Category.objects.all().only("pk", "parent_id", "kind")}

    def root_kind(category: Any) -> str:
        while category.parent_id is not None:
            category = rows[category.parent_id]
        return category.kind

    for category in rows.values():
        kind = root_kind(category)
        if category.kind != kind:
            Category.objects.filter(pk=category.pk).update(kind=kind)


def backfill_embeddings(apps: Any, schema_editor: Any) -> None:
    from embeddings import engine
    from finance.textnorm import transaction_text

    if not engine.enabled():
        return
    current = engine.model_id()
    sources_of = {
        "Category": (("name", "description"), lambda row: engine.source_text(row.name, row.description)),
        "Transaction": (("counterparty", "remittance", "kind"), lambda row: transaction_text(row.counterparty, row.remittance, row.kind)),
    }
    for model_name, (fields, source_of) in sources_of.items():
        model = apps.get_model("finance", model_name)
        queryset = model.objects.exclude(embedding_model=current).order_by("pk").only("pk", *fields)
        while True:
            rows = list(queryset[:BATCH])
            if not rows:
                break
            sources = [source_of(row) for row in rows]
            vectors = iter(engine.embed_texts([source for source in sources if source is not None]))
            for row, source in zip(rows, sources, strict=True):
                row.embedding = next(vectors) if source is not None else None
                row.embedding_model = current
            model.objects.bulk_update(rows, ["embedding", "embedding_model"])


class Migration(migrations.Migration):

    dependencies = [
        ('authentikate', '0007_user_profile'),
        ('finance', '0003_on_demand_sync'),
    ]

    operations = [
        VectorExtension(),
        migrations.CreateModel(
            name='DismissedBaseCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(help_text='The dismissed base-taxonomy key.', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True, help_text='When it was dismissed.')),
            ],
        ),
        migrations.AddField(
            model_name='category',
            name='description',
            field=models.TextField(blank=True, default='', help_text='What belongs here, in words a bank line would use. Embedded with the name for suggestions.'),
        ),
        migrations.AddField(
            model_name='category',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=256, editable=False, help_text='Unit-length embedding of name + description, by the model named in embedding_model; NULL when there is no text to embed', null=True),
        ),
        migrations.AddField(
            model_name='category',
            name='embedding_model',
            field=models.CharField(blank=True, default='', editable=False, help_text='The embedding model that produced `embedding`. Rows whose value differs from the configured model are re-embedded in-process and are excluded from vector search until then', max_length=200),
        ),
        migrations.AddField(
            model_name='category',
            name='hidden',
            field=models.BooleanField(default=False, help_text='Hidden from pickers, suggestions and automatic assignment; its transactions keep it.'),
        ),
        migrations.AddField(
            model_name='category',
            name='key',
            field=models.CharField(blank=True, help_text="The base-taxonomy key (``food.groceries``) of a base category; null for the organization's own.", max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='historicalcategory',
            name='description',
            field=models.TextField(blank=True, default='', help_text='What belongs here, in words a bank line would use. Embedded with the name for suggestions.'),
        ),
        migrations.AddField(
            model_name='historicalcategory',
            name='hidden',
            field=models.BooleanField(default=False, help_text='Hidden from pickers, suggestions and automatic assignment; its transactions keep it.'),
        ),
        migrations.AddField(
            model_name='historicalcategory',
            name='key',
            field=models.CharField(blank=True, help_text="The base-taxonomy key (``food.groceries``) of a base category; null for the organization's own.", max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='transaction',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=256, editable=False, help_text='Unit-length embedding of name + description, by the model named in embedding_model; NULL when there is no text to embed', null=True),
        ),
        migrations.AddField(
            model_name='transaction',
            name='embedding_model',
            field=models.CharField(blank=True, default='', editable=False, help_text='The embedding model that produced `embedding`. Rows whose value differs from the configured model are re-embedded in-process and are excluded from vector search until then', max_length=200),
        ),
        migrations.AlterField(
            model_name='category',
            name='kind',
            field=models.CharField(choices=[('EXPENSE', 'Expense'), ('INCOME', 'Income'), ('TRANSFER', 'Transfer between own accounts; excluded from stats')], default='EXPENSE', help_text="Whether money in this category is an expense, income, or a transfer between own accounts. Always the root's kind.", max_length=20),
        ),
        migrations.AlterField(
            model_name='historicalcategory',
            name='kind',
            field=models.CharField(choices=[('EXPENSE', 'Expense'), ('INCOME', 'Income'), ('TRANSFER', 'Transfer between own accounts; excluded from stats')], default='EXPENSE', help_text="Whether money in this category is an expense, income, or a transfer between own accounts. Always the root's kind.", max_length=20),
        ),
        migrations.AlterField(
            model_name='transaction',
            name='category_source',
            field=models.CharField(choices=[('NONE', 'Uncategorized'), ('RULE', 'Set by a rule'), ('MANUAL', 'Set by a user; rules never touch it'), ('SEMANTIC', 'Set because similar, already categorized transactions agreed; rules and users override it')], default='NONE', help_text='Who set the category; rules never override a manual one.', max_length=10),
        ),
        migrations.AddIndex(
            model_name='category',
            index=models.Index(fields=['embedding_model'], name='bank_cat_emb_model_idx'),
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['embedding_model'], name='bank_tx_emb_model_idx'),
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.UniqueConstraint(condition=models.Q(('key__isnull', False)), fields=('organization', 'key'), name='bank_cat_org_key'),
        ),
        migrations.AddField(
            model_name='dismissedbasecategory',
            name='organization',
            field=models.ForeignKey(help_text='The organization.', on_delete=django.db.models.deletion.CASCADE, related_name='bank_dismissed_categories', to='authentikate.organization'),
        ),
        migrations.AddConstraint(
            model_name='dismissedbasecategory',
            constraint=models.UniqueConstraint(fields=('organization', 'key'), name='bank_dismissed_org_key'),
        ),
            migrations.RunPython(key_legacy_defaults, migrations.RunPython.noop),
        migrations.RunPython(inherit_root_kinds, migrations.RunPython.noop),
        migrations.RunPython(backfill_embeddings, migrations.RunPython.noop),
    ]
