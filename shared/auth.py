from django.contrib.auth import authenticate, login as session_login, logout as session_logout
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import viewsets, status, serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle


@method_decorator(csrf_protect, name='dispatch')
class SessionViewSet(viewsets.ViewSet):
    throttle_scope = None
    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    @action(detail=False, methods=['get'], url_path='status')
    def session_status(self, request):
        return Response({'authenticated':request.user.is_authenticated,
            'username':request.user.get_username() if request.user.is_authenticated else None,
            'csrf_token':get_token(request)})

    @action(detail=False, methods=['post'], throttle_classes=[ScopedRateThrottle], throttle_scope='login')
    def login(self, request):
        class Credentials(serializers.Serializer):
            username=serializers.CharField(max_length=150)
            password=serializers.CharField(max_length=4096,trim_whitespace=False)
        credentials=Credentials(data=request.data)
        credentials.is_valid(raise_exception=True)
        user = authenticate(request, **credentials.validated_data)
        if user is None:
            return Response({'error':'Invalid username or password.'},status=status.HTTP_401_UNAUTHORIZED)
        session_login(request, user)
        return Response({'authenticated':True,'username':user.get_username(),'csrf_token':get_token(request)})

    @action(detail=False, methods=['post'])
    def logout(self, request):
        session_logout(request)
        return Response({'authenticated':False,'csrf_token':get_token(request)})
