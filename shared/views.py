from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Settings, Contact, PaymentAccount
from .serializers import SettingsSerializer, ContactSerializer, PaymentAccountSerializer


class SettingsViewSet(viewsets.ModelViewSet):
    serializer_class = SettingsSerializer

    def get_queryset(self):
        return Settings.objects.all()

    def get_object(self):
        return Settings.get()

    def get_serializer_context(self):
        # Ensures request is passed into serializer for building absolute URLs
        context = super().get_serializer_context()
        return context

    def list(self, request, *args, **kwargs):
        return Response(SettingsSerializer(self.get_object(), context={'request': request}).data)

    def create(self, request, *args, **kwargs):
        return self._upsert(request)

    def update(self, request, *args, **kwargs):
        return self._upsert(request)

    def partial_update(self, request, *args, **kwargs):
        return self._upsert(request)

    def _upsert(self, request):
        instance   = self.get_object()
        serializer = SettingsSerializer(instance, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ContactViewSet(viewsets.ModelViewSet):
    serializer_class = ContactSerializer
    search_fields    = ['contact_name', 'company_name', 'phone']
    ordering_fields  = ['contact_name', 'company_name', 'created_at']
    ordering         = ['contact_name']

    def get_queryset(self):
        qs        = Contact.objects.all()
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')
        return qs

    @action(detail=True, methods=['get'])
    def ledger(self, request, pk=None):
        contact = self.get_object()
        from accounting.models import FinancialTransaction
        from accounting.serializers import FinancialTransactionSerializer
        txns = FinancialTransaction.objects.filter(
            contact=contact
        ).order_by('date', 'created_at')
        return Response(FinancialTransactionSerializer(txns, many=True, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        contact = self.get_object()
        from accounting.services import process_send_receive
        result = process_send_receive(contact, request.data, direction='send')
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        contact = self.get_object()
        from accounting.services import process_send_receive
        result = process_send_receive(contact, request.data, direction='receive')
        return Response(result, status=status.HTTP_201_CREATED)


class PaymentAccountViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentAccountSerializer
    ordering         = ['name']

    def get_queryset(self):
        qs        = PaymentAccount.objects.all()
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')
        return qs

    @action(detail=False, methods=['post'])
    def transfer(self, request):
        from accounting.services import process_transfer
        result = process_transfer(request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def adjust(self, request, pk=None):
        account = self.get_object()
        from accounting.services import process_adjust_balance
        result = process_adjust_balance(account, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def set_balance(self, request, pk=None):
        account = self.get_object()
        balance = request.data.get('current_balance')
        if balance is None:
            return Response({'error': 'current_balance required.'}, status=status.HTTP_400_BAD_REQUEST)
        # Per spec B1 — direct overwrite, no f.txn generated
        account.current_balance = balance
        account.save(update_fields=['current_balance', 'updated_at'])
        return Response(PaymentAccountSerializer(account).data)
