# upload/views.py
from pathlib import Path
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from .utils import process_upload


class UploadViewSet(ViewSet):
    """
    POST /api/upload/file/
    Accepts: multipart/form-data, key = 'file'
    Query param: ?type=documents|products|settings  (default: documents)
    Returns: { "path": "uploads/documents/abc.jpg", "url": "http://..." }
    """

    @action(detail=False, methods=['post'], url_path='file')
    def file(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

        subfolder = request.query_params.get('type', 'documents')
        if subfolder not in ('documents', 'products', 'settings'):
            subfolder = 'documents'

        try:
            file_data, relative_path = process_upload(file, subfolder=subfolder)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        full_path = Path(settings.MEDIA_ROOT) / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(file_data)

        url = request.build_absolute_uri(f"{settings.MEDIA_URL}{relative_path}")
        return Response({'path': relative_path, 'url': url}, status=status.HTTP_201_CREATED)
