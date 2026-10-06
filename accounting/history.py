"""Prospective saved document versions, independent of filing status."""
import json
from django.core.serializers.json import DjangoJSONEncoder
from .models import DocumentRevision


def capture_revision(document, event):
    fields = [field.attname for field in document._meta.concrete_fields if field.name not in ('id', 'created_at', 'updated_at')]
    snapshot = json.loads(json.dumps({field: getattr(document, field) for field in fields}, cls=DjangoJSONEncoder))
    previous = document.revisions.first()
    if previous and previous.snapshot == snapshot:
        return
    changed = [field for field in fields if previous and previous.snapshot.get(field) != snapshot.get(field)]
    if 'is_active' in changed:
        event = 'archived' if not document.is_active else 'updated'
    DocumentRevision.objects.create(document=document, event=event, snapshot=snapshot, changed_fields=changed)
