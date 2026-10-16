"""Independent CP bindings with kind/provider-scoped content identities."""
import base64
from copy import deepcopy
from .media import CockpitError, LocalMediaCatalog, TencentMusicGateway, media_item

KINDS = ('music', 'video', 'radio')


class HTTPMediaGateway(TencentMusicGateway):
    """Operator-defined CP gateway protocol, not a vendor's purported public API."""
    def __init__(self, *, provider_name, kind, base_url='', api_key='', client=None, timeout_seconds=12, require_api_key=True):
        if kind not in KINDS or not isinstance(provider_name, str) or not provider_name:
            raise CockpitError('invalid_configuration', 'CP gateway requires a provider name and content kind')
        super().__init__(base_url=base_url, api_key=api_key, client=client, timeout_seconds=timeout_seconds, require_api_key=require_api_key)
        self.provider_name, self.kind = provider_name, kind

    def info(self):
        return {**super().info(), 'provider': self.provider_name, 'kinds': [self.kind], 'execution_location': 'cloud'}

    async def search(self, *, kind, query, max_results=5):
        if kind != self.kind:
            raise CockpitError('unsupported_kind', 'CP gateway does not serve this content kind')
        result = await super().search(kind=kind, query=query, max_results=max_results)
        result['provider'] = self.provider_name
        for item in result['items']: item['provider'] = self.provider_name
        return result

    async def resolve(self, content_id):
        item = await super().resolve(content_id)
        if item['kind'] != self.kind:
            raise CockpitError('invalid_content', 'CP gateway resolved a different content kind')
        item['provider'] = self.provider_name
        return item


class MediaServices:
    def __init__(self, providers_by_kind, bindings=None):
        if set(providers_by_kind) != set(KINDS):
            raise CockpitError('invalid_configuration', 'Music, video and radio require independent provider bindings')
        self.providers = dict(providers_by_kind)
        self.bindings = dict(bindings or {kind: provider.info()['provider'] for kind,provider in self.providers.items()})
        if set(self.bindings) != set(KINDS) or any(not isinstance(value,str) or not value or ':' in value or len(value)>80 for value in self.bindings.values()):
            raise CockpitError('invalid_configuration', 'Invalid CP profile bindings')
        for kind,provider in self.providers.items():
            if kind not in provider.info().get('kinds', KINDS):
                raise CockpitError('unsupported_kind', 'Provider cannot serve its bound content kind')

    def provider_info(self, kind):
        if kind not in self.providers:
            raise CockpitError('unsupported_kind', 'Unknown content kind')
        return {**self.providers[kind].info(), 'binding':self.bindings[kind], 'kind':kind}

    def info(self):
        return {'provider':'media_services','bindings':deepcopy(self.bindings),
                'providers':{kind:self.provider_info(kind) for kind in KINDS},
                'configured':any(self.provider_info(kind).get('configured') for kind in KINDS)}

    def scoped_id(self, kind, raw):
        self.provider_info(kind)
        if not isinstance(raw,str) or not raw or len(raw)>200:
            raise CockpitError('invalid_content', 'CP content ID is invalid')
        opaque=base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')
        identifier = f'media:{kind}:{self.bindings[kind]}:{opaque}'
        if len(identifier) > 500:
            raise CockpitError('invalid_content', 'Scoped CP content ID exceeds the tool limit')
        return identifier

    def parse_id(self, content_id):
        if not isinstance(content_id,str) or len(content_id) > 500:
            raise CockpitError('unknown_content', 'Expected a scoped CP content ID')
        parts=content_id.split(':')
        if len(parts)!=4 or parts[0]!='media' or parts[1] not in self.providers or self.bindings[parts[1]]!=parts[2]:
            raise CockpitError('unknown_content', 'Content does not belong to an active CP binding')
        try:
            raw=base64.b64decode(parts[3]+'='*(-len(parts[3])%4),altchars=b'-_',validate=True).decode()
        except (ValueError,UnicodeError):
            raise CockpitError('unknown_content', 'Invalid scoped CP content ID') from None
        if self.scoped_id(parts[1],raw)!=content_id:
            raise CockpitError('unknown_content', 'Noncanonical scoped CP content ID')
        return parts[1],parts[2],raw

    def content_provider_info(self, content_id):
        kind,_,_=self.parse_id(content_id)
        return self.provider_info(kind)

    def device_content_id(self, content_id):
        return self.parse_id(content_id)[2]

    def _scoped_item(self, kind, original, *, playable=False):
        item=media_item(original,playable=playable)
        if item['kind']!=kind:
            raise CockpitError('invalid_content', 'CP returned another kind of content')
        item['content_id']=self.scoped_id(kind,item['content_id'])
        item['provider']=self.provider_info(kind)['provider']
        return item

    def scope_device_result(self, kind, result):
        self.provider_info(kind)
        if not isinstance(result, dict):
            raise CockpitError('invalid_content', 'Device media result must be an object')
        scoped=deepcopy(result)
        if 'items' in result:
            if not isinstance(result['items'],list):raise CockpitError('invalid_content','Device media items must be an array')
            scoped['items']=[self._scoped_item(kind,item) for item in result['items']]
        if 'item' in result: scoped['item']=self._scoped_item(kind,result['item'])
        scoped.update(provider=self.provider_info(kind)['provider'],binding=self.bindings[kind])
        return scoped

    async def search(self, *,kind,query,max_results=5):
        self.provider_info(kind)
        result=await self.providers[kind].search(kind=kind,query=query,max_results=max_results)
        if not isinstance(result,dict) or result.get('ok') is not True or not isinstance(result.get('items'),list):
            raise CockpitError('invalid_content','CP search response is invalid')
        return {**result,'items':[self._scoped_item(kind,item) for item in result['items']],
                'provider':self.provider_info(kind)['provider'],'binding':self.bindings[kind]}

    async def resolve(self, content_id):
        kind,_,raw=self.parse_id(content_id)
        item=await self.providers[kind].resolve(raw)
        if not isinstance(item, dict) or item.get('content_id')!=raw:
            raise CockpitError('invalid_content','CP resolved a different content ID')
        scoped = self._scoped_item(kind,item,playable=True)
        if '_provider_trace' in item: scoped['_provider_trace'] = deepcopy(item['_provider_trace'])
        return scoped

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

    async def aclose(self):
        seen=set()
        for provider in self.providers.values():
            if id(provider) not in seen:
                seen.add(id(provider));await provider.aclose()
