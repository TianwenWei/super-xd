"""Explicit local media catalog for the Demo."""
from copy import deepcopy
from super_xd.nlp.contracts.media import CockpitError, KINDS, media_item


class LocalMediaCatalog:
    """Development catalog is opt-in and never stands in for Tencent content."""
    def __init__(self, entries=()):
        self.entries = {}
        for value in entries:
            item = media_item(value, playable=True)
            if item['content_id'] in self.entries:
                raise CockpitError('duplicate_content', 'Duplicate media content_id')
            item.setdefault('provider', 'local_catalog')
            self.entries[item['content_id']] = item

    def info(self):
        return {'provider': 'local_catalog', 'configured': bool(self.entries), 'mode': 'explicit_development', 'count': len(self.entries), 'execution_location': 'cloud'}

    async def search(self, *, kind, query, max_results=5):
        if kind not in KINDS or not isinstance(query, str) or not query.strip():
            raise CockpitError('invalid_query', 'Media search requires kind and query')
        if type(max_results) is not int or not 1 <= max_results <= 10:
            raise CockpitError('invalid_query', 'Media search result limit is invalid')
        words = query.casefold().split()
        # This is catalog retrieval over a structured query, never intent routing.
        ranked = []
        for item in self.entries.values():
            if item['kind'] != kind:
                continue
            searchable = ' '.join(str(item.get(key, '')) for key in ('title', 'artist', 'album')).casefold()
            score = sum(word in searchable for word in words)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda row: (-row[0], row[1]['content_id']))
        return {'ok': True, 'kind': 'media', 'provider': 'local_catalog', 'items': [media_item(item) for _, item in ranked[:max_results]], 'query': query}

    async def resolve(self, content_id):
        if content_id not in self.entries:
            raise CockpitError('unknown_content', 'Content is not in the configured catalog')
        return deepcopy(self.entries[content_id])

    async def aclose(self):
        pass



from super_xd.nlp.adapters.media.catalogs import MediaServices


class DemoMediaServices(MediaServices):
    """Expose local file entries only to the Demo HTTP media endpoint."""
    @property
    def entries(self):
        result={}
        for kind,provider in self.providers.items():
            if isinstance(provider,LocalMediaCatalog):
                for item in provider.entries.values():
                    if item['kind']==kind:
                        scoped=self._scoped_item(kind,item,playable=True)
                        result[scoped['content_id']]=scoped
        return result
