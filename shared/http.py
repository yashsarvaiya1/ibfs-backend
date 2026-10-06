from pathlib import Path
from django.conf import settings
from django.http import FileResponse, JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny


@api_view(['GET'])
@permission_classes([AllowAny])
def private_media(request, path):
    if not request.user.is_authenticated:
        return JsonResponse({'error':'Authentication required.'},status=401)
    root = Path(settings.MEDIA_ROOT).resolve()
    file = (root/path).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        return JsonResponse({'error':'File not found.'},status=404)
    response = FileResponse(file.open('rb'))
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def health(request):
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except Exception:
        return JsonResponse({'status':'unavailable'},status=503)
    return JsonResponse({'status':'ok'})


class PrivateResponseMiddleware:
    def __init__(self,get_response):
        self.get_response=get_response

    def __call__(self,request):
        response=self.get_response(request)
        if request.path.startswith(('/api/','/media/')):
            response['Cache-Control']='private, no-store'
        return response
