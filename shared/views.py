# shared/views.py
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Sum
from .models import Settings, Contact, PaymentAccount
from .serializers import SettingsSerializer, ContactSerializer, PaymentAccountSerializer


class SettingsViewSet(viewsets.ModelViewSet):
    serializer_class = SettingsSerializer

    def get_queryset(self):
        return Settings.objects.all()

    def get_object(self):
        return Settings.get()

    def get_serializer_context(self):
        return super().get_serializer_context()

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

    def destroy(self, request, *args, **kwargs):
        contact = self.get_object()
        contact.is_active = False
        contact.save(update_fields=['is_active', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'])
    def ledger(self, request, pk=None):
        """
        Paginated contact ledger — chronological order (oldest → newest per page).

        Fixes applied:
          BF-02 — was raw Response(), now uses paginate_queryset + get_paginated_response
          TV-01 — ledger is chronological (oldest first), not reversed
          TV-02 — supports filters: date_from, date_to, type, account
          TV-03 — pagination wired via IBFSPageNumberPagination
          TV-05 — returns opening_balance_at = CF just before the filtered window

        Response envelope (extends IBFSPageNumberPagination):
          {
            "count": ..., "total_pages": ..., "current_page": ...,
            "page_size": ..., "next": ..., "previous": ...,
            "opening_balance_at": "500.00",   ← CF before first filtered txn
            "results": [...]
          }
        """
        from accounting.models import FinancialTransaction
        from accounting.serializers import FinancialTransactionSerializer

        contact = self.get_object()
        params  = request.query_params

        # ── Build queryset — chronological order for ledger (TV-03) ──────────
        qs = (
            FinancialTransaction.objects
            .filter(contact=contact)
            .select_related('document', 'payment_account')
            .order_by('date', 'created_at')             # oldest → newest for ledger
        )

        # ── Filters (TV-02) ───────────────────────────────────────────────────
        date_from = params.get('date_from')
        date_to   = params.get('date_to')

        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if params.get('type') not in (None, ''):
            qs = qs.filter(type=params['type'])
        if params.get('account') not in (None, ''):
            qs = qs.filter(payment_account_id=params['account'])

        # ── TV-05: Compute CF just before the filtered window ─────────────────
        # opening_balance_at = contact.opening_balance
        #   + sum of all non-expense txns with date < date_from
        # If no date_from → opening_balance_at = contact.opening_balance only
        opening_balance_at = contact.opening_balance
        if date_from:
            pre_sum = (
                FinancialTransaction.objects
                .filter(contact=contact, date__lt=date_from)
                .exclude(document__type='expense')
                .select_related('document')
                .aggregate(total=Sum('amount'))['total'] or Decimal('0')
            )
            opening_balance_at = contact.opening_balance + pre_sum

        # ── BF-02: proper pagination ──────────────────────────────────────────
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = FinancialTransactionSerializer(page, many=True, context={'request': request})
            response   = self.get_paginated_response(serializer.data)
            response.data['opening_balance_at'] = str(opening_balance_at.quantize(Decimal('0.01')))
            return response

        serializer = FinancialTransactionSerializer(qs, many=True, context={'request': request})
        return Response({
            'opening_balance_at': str(opening_balance_at.quantize(Decimal('0.01'))),
            'results':            serializer.data,
        })

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

    def destroy(self, request, *args, **kwargs):
        account = self.get_object()
        account.is_active = False
        account.save(update_fields=['is_active', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

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
            return Response(
                {'error': 'current_balance required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        account.current_balance = balance
        account.save(update_fields=['current_balance', 'updated_at'])
        return Response(PaymentAccountSerializer(account).data)

    @action(detail=True, methods=['get'])
    def transactions(self, request, pk=None):
        """
        Paginated transaction history for a payment account.
        Newest first (TV-01). Supports filters: date_from, date_to, type, contact.
        Returns balance_before_period = account balance just before the filtered window.

        Response envelope:
          {
            "count": ..., "total_pages": ..., "current_page": ...,
            "page_size": ..., "next": ..., "previous": ...,
            "balance_before_period": "9600.00",
            "results": [...]
          }
        """
        from accounting.models import FinancialTransaction
        from accounting.serializers import FinancialTransactionSerializer

        account = self.get_object()
        params  = request.query_params

        qs = (
            FinancialTransaction.objects
            .filter(payment_account=account)
            .select_related('document', 'contact')
            .order_by('-date', '-created_at')           # TV-01: newest first
        )

        date_from = params.get('date_from')
        date_to   = params.get('date_to')

        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if params.get('type') not in (None, ''):
            qs = qs.filter(type=params['type'])
        if params.get('contact') not in (None, ''):
            qs = qs.filter(contact_id=params['contact'])

        # balance_before_period = current_balance − sum of txns from date_from onwards
        # so frontend can show the opening balance for the printed/viewed period
        balance_before = account.current_balance
        if date_from:
            from_sum = (
                FinancialTransaction.objects
                .filter(payment_account=account, date__gte=date_from)
                .aggregate(total=Sum('amount'))['total'] or Decimal('0')
            )
            balance_before = account.current_balance - from_sum

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = FinancialTransactionSerializer(page, many=True, context={'request': request})
            response   = self.get_paginated_response(serializer.data)
            response.data['balance_before_period'] = str(Decimal(str(balance_before)).quantize(Decimal('0.01')))
            return response

        serializer = FinancialTransactionSerializer(qs, many=True, context={'request': request})
        return Response({
            'balance_before_period': str(Decimal(str(balance_before)).quantize(Decimal('0.01'))),
            'results':               serializer.data,
        })
