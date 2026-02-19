from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .models import Contact
from .serializers import ContactSerializer


class ContactViewSet(viewsets.ModelViewSet):
    serializer_class = ContactSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['company_name', 'contact_name', 'phone', 'gstin']

    def get_queryset(self):
        # Default: only active contacts
        # Pass ?include_inactive=true to get all
        include_inactive = self.request.query_params.get('include_inactive', 'false')
        if include_inactive.lower() == 'true':
            return Contact.objects.all()
        return Contact.objects.filter(is_active=True)

    def destroy(self, request, *args, **kwargs):
        # Soft delete — never actually deletes the record
        # Preserves FK references in Documents and Transactions
        contact = self.get_object()
        contact.is_active = False
        contact.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
