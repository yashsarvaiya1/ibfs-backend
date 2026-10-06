from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from .models import Product


class AdjustmentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('stock'))
        self.product = Product.objects.create(name='Item',rate=10,current_stock=5)

    def test_direct_stock_correction_keeps_history(self):
        response = self.client.patch(f'/api/products/{self.product.pk}/',{'current_stock':'6.50'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(response.data['current_stock']),6.5)
        self.assertEqual(float(self.product.stock_transactions.get().quantity),1.5)

    def test_invalid_adjustments_return_validation_errors(self):
        for quantity in ('NaN',0,'not a number'):
            response = self.client.post(f'/api/products/{self.product.pk}/adjust_stock/',{'quantity':quantity},format='json')
            self.assertEqual(response.status_code,400,response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock,5)
