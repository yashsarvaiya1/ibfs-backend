# shared/pagination.py
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class IBFSPageNumberPagination(PageNumberPagination):
    """
    Global pagination class for all IBFS viewsets.

    Supported query params:
      ?page=2          — page number (default: 1)
      ?page_size=50    — rows per page (default: 20, max: 100) [GD-03]

    Response envelope:
      {
        "count":        <total rows>,
        "total_pages":  <total pages>,
        "current_page": <current page number>,
        "page_size":    <active page size>,
        "next":         <url or null>,
        "previous":     <url or null>,
        "results":      [...]
      }
    """
    page_size              = 20
    page_size_query_param  = 'page_size'
    max_page_size          = 100

    def get_paginated_response(self, data):
        return Response({
            'count':        self.page.paginator.count,
            'total_pages':  self.page.paginator.num_pages,
            'current_page': self.page.number,
            'page_size':    self.get_page_size(self.request),
            'next':         self.get_next_link(),
            'previous':     self.get_previous_link(),
            'results':      data,
        })

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'properties': {
                'count':        {'type': 'integer'},
                'total_pages':  {'type': 'integer'},
                'current_page': {'type': 'integer'},
                'page_size':    {'type': 'integer'},
                'next':         {'type': 'string', 'nullable': True},
                'previous':     {'type': 'string', 'nullable': True},
                'results':      schema,
            },
        }
